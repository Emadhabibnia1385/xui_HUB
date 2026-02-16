# bot.py
# python-telegram-bot 21.6
# Server Manager + Merge + Backup (فعال)
# Start بدون SSH/DB
import os
import json
import re
import asyncio
import tempfile
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List

import paramiko
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------------------- Logging -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("xuihub")

STORE_FILE = "store.json"
ENV_FALLBACK_PATH = "/opt/xui_HUB/.env"

# ------------------------- DB find command (safe, timed) -------------------------
FIND_DB_CMD = r"""
set -e
for p in /etc/x-ui/x-ui.db /usr/local/x-ui/x-ui.db /opt/x-ui/x-ui.db /var/lib/x-ui/x-ui.db /root/x-ui.db; do
  if [ -f "$p" ]; then echo "$p"; exit 0; fi
done

if command -v timeout >/dev/null 2>&1; then
  DB=$(timeout 12s sudo find / -maxdepth 6 -name "x-ui.db" 2>/dev/null | head -n 1 || true)
else
  DB=$(sudo find / -maxdepth 6 -name "x-ui.db" 2>/dev/null | head -n 1 || true)
fi

if [ -z "$DB" ]; then
  echo "NOT_FOUND"
else
  echo "$DB"
fi
"""

# ------------------------- Helpers: ENV -------------------------
def load_env_file(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        logger.exception("Failed reading .env")
    return out


def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if v:
        return v
    envs = load_env_file(ENV_FALLBACK_PATH)
    v2 = (envs.get(name) or "").strip()
    if v2:
        return v2
    raise RuntimeError(f"Missing env: {name}")


# ------------------------- Storage -------------------------
def load_store() -> Dict[str, Any]:
    if not os.path.exists(STORE_FILE):
        return {"users": {}}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}


def save_store(data: Dict[str, Any]) -> None:
    try:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("save_store failed")


def get_user_bucket(store: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    uid = str(user_id)
    store.setdefault("users", {})
    store["users"].setdefault(uid, {"servers": {}, "order": []})
    b = store["users"][uid]
    b.setdefault("servers", {})
    b.setdefault("order", [])
    return b


def safe_server_id(ip: str) -> str:
    # simple ID based on ip
    sid = re.sub(r"[^0-9.]+", "", ip.strip())
    return sid or "server"


def is_ipv4(s: str) -> bool:
    s = s.strip()
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except Exception:
        return False
    return all(0 <= n <= 255 for n in nums)


def validate_port(v: str) -> Optional[int]:
    try:
        p = int(v.strip())
        if 1 <= p <= 65535:
            return p
        return None
    except Exception:
        return None


# ------------------------- SSH helpers (safe + timeouts) -------------------------
def ssh_client(host: str, port: int, user: str, password: str, timeout: int = 20) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    return c


def ssh_exec_raw(c: paramiko.SSHClient, cmd: str, read_timeout: int = 35) -> Tuple[int, str, str]:
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    try:
        stdout.channel.settimeout(read_timeout)
        stderr.channel.settimeout(read_timeout)
    except Exception:
        pass
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def ssh_exec(
    host: str,
    port: int,
    user: str,
    password: str,
    cmd: str,
    conn_timeout: int = 20,
    read_timeout: int = 35,
) -> Tuple[int, str, str]:
    c = ssh_client(host, port, user, password, timeout=conn_timeout)
    try:
        return ssh_exec_raw(c, cmd, read_timeout=read_timeout)
    finally:
        c.close()


# ------------------------- UI Text -------------------------
START_TEXT = (
    "🤖 **به xuiHUB خوش آمدید**\n\n"
    "xuiHUB یک ربات حرفه‌ای برای مدیریت پنل‌های 3x-ui / x-ui است.\n"
    "از داخل تلگرام می‌توانید سرورها، پورت‌ها، کانفیگ‌ها و بکاپ‌ها را مدیریت کنید.\n\n"
    "برای شروع، از منوی زیر استفاده کنید 👇\n\n"
    "👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)

def kb_main() -> InlineKeyboardMarkup:
    # دو دکمه کنار هم: پورت/کانفیگ + بکاپ
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥 مدیریت سرورها", callback_data="server_manager")],
        [
            InlineKeyboardButton("🔀 مدیریت پورت و کانفیگ", callback_data="start_merge"),
            InlineKeyboardButton("🗂 مدیریت بکاپ", callback_data="backup_menu"),
        ],
        [InlineKeyboardButton("👤 پروفایل", callback_data="profile")],
    ])

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])

def kb_yes_no_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ تایید", callback_data="add_panel_yes"),
                                 InlineKeyboardButton("❌ خیر", callback_data="add_panel_no")]])

def kb_panel_scheme() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔒 HTTP", callback_data="scheme:http"),
                                 InlineKeyboardButton("🔐 HTTPS", callback_data="scheme:https")]])

def kb_server_details_actions(server_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ ویرایش اطلاعات", callback_data=f"server_edit:{server_id}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"server_del:{server_id}"),
        ],
        [InlineKeyboardButton("📌 ثبت/ویرایش پورت‌ها", callback_data=f"ports_setup:{server_id}")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="server_manager")],
    ])

def kb_server_edit_menu(server_id: str, has_panel: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("ویرایش IP", callback_data=f"edit_field:{server_id}:ip")],
        [InlineKeyboardButton("ویرایش SSH User", callback_data=f"edit_field:{server_id}:ssh_user")],
        [InlineKeyboardButton("ویرایش SSH Pass", callback_data=f"edit_field:{server_id}:ssh_pass")],
        [InlineKeyboardButton("ویرایش SSH Port", callback_data=f"edit_field:{server_id}:ssh_port")],
    ]
    if has_panel:
        rows += [
            [InlineKeyboardButton("ویرایش دامنه", callback_data=f"edit_field:{server_id}:panel.domain")],
            [InlineKeyboardButton("ویرایش HTTP/HTTPS", callback_data=f"edit_scheme:{server_id}")],
            [InlineKeyboardButton("ویرایش پورت پنل", callback_data=f"edit_field:{server_id}:panel.panel_port")],
            [InlineKeyboardButton("ویرایش Path", callback_data=f"edit_field:{server_id}:panel.panel_path")],
            [InlineKeyboardButton("ویرایش User پنل", callback_data=f"edit_field:{server_id}:panel.panel_user")],
            [InlineKeyboardButton("ویرایش Pass پنل", callback_data=f"edit_field:{server_id}:panel.panel_pass")],
        ]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data=f"server_details:{server_id}")])
    return InlineKeyboardMarkup(rows)

def _panel_button_label(server: Dict[str, Any]) -> str:
    ip = server.get("ip", "unknown")
    panel = server.get("panel") or {}
    dom = (panel.get("domain") or "").strip()
    return f"{ip} ({dom})" if dom else ip

def kb_server_manager(store: Dict[str, Any], user_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(store, user_id)
    rows: List[List[InlineKeyboardButton]] = [[InlineKeyboardButton("➕ افزودن سرور", callback_data="server_add")]]
    for sid in bucket.get("order", []):
        s = bucket["servers"].get(sid)
        if not s:
            continue
        rows.append([InlineKeyboardButton(_panel_button_label(s), callback_data=f"server_details:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_pick_server_for_action(store: Dict[str, Any], user_id: int, prefix: str, title_btn: str) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(store, user_id)
    rows: List[List[InlineKeyboardButton]] = []
    for sid in bucket.get("order", []):
        s = bucket["servers"].get(sid)
        if not s:
            continue
        rows.append([InlineKeyboardButton(_panel_button_label(s), callback_data=f"{prefix}:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_backup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 گرفتن بکاپ", callback_data="bk_export")],
        [InlineKeyboardButton("📥 وارد کردن بکاپ", callback_data="bk_import")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")],
    ])

def kb_backup_import_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 از سرورهای موجود", callback_data="bk_import_existing")],
        [InlineKeyboardButton("➕ سرور جدید (بدون ذخیره)", callback_data="bk_import_new")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")],
    ])

# ------------------------- Jalali helpers (for backup caption) -------------------------
def gregorian_to_jalali(gy: int, gm: int, gd: int) -> Tuple[int, int, int]:
    g_d_m = [0,31,59,90,120,151,181,212,243,273,304,334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621
    gy2 = gy + 1 if gm > 2 else gy
    days = (365*gy) + ((gy2+3)//4) - ((gy2+99)//100) + ((gy2+399)//400) - 80 + gd + g_d_m[gm-1]
    jy += 33*(days//12053)
    days %= 12053
    jy += 4*(days//1461)
    days %= 1461
    if days > 365:
        jy += (days-1)//365
        days = (days-1) % 365
    if days < 186:
        jm = 1 + (days//31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days-186)//30)
        jd = 1 + ((days-186) % 30)
    return jy, jm, jd

PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
def to_fa_digits(s: str) -> str:
    return s.translate(PERSIAN_DIGITS)

def build_backup_caption(panel_addr: str, now_utc: datetime) -> str:
    g_date = now_utc.strftime("%Y-%m-%d")
    g_time = now_utc.strftime("%H:%M UTC")
    tehran = now_utc + timedelta(hours=3, minutes=30)
    jy, jm, jd = gregorian_to_jalali(tehran.year, tehran.month, tehran.day)
    j_date = f"{jy:04d}/{jm:02d}/{jd:02d}"
    j_time = tehran.strftime("%H:%M")
    return (
        f"🗂 بکاپ پنل: {panel_addr}\n\n"
        f"📅 تاریخ (میلادی): {g_date}\n"
        f"⏰ ساعت: {g_time}\n\n"
        f"📆 تاریخ (شمسی): {to_fa_digits(j_date)}\n"
        f"⏱ ساعت: {to_fa_digits(j_time)}\n\n"
        f"📦 نوع بکاپ: Full x-ui Database\n\n"
        f"🤖 xuiHUB\n"
        f"👨‍💻 Developer: @EmadHabibnia"
    )

# ------------------------- Formatting: ONLY values copyable -------------------------
def fmt_kv(label: str, value: str) -> str:
    # Label plain, only value in backticks
    return f"{label} `{value}`"

def _fmt_panel_url(panel: Dict[str, Any]) -> str:
    scheme = (panel.get("scheme") or "http").strip()
    dom = (panel.get("domain") or "").strip()
    pport = panel.get("panel_port") or 0
    ppath = (panel.get("panel_path") or "/").strip()
    if not ppath.startswith("/"):
        ppath = "/" + ppath
    if not dom:
        dom = "0.0.0.0"
    return f"{scheme}://{dom}:{pport}{ppath}"

def _short(s: str, n: int = 1600) -> str:
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")

# ------------------------- States -------------------------
(
    SRV_ADD_IP,
    SRV_ADD_SSH_USER,
    SRV_ADD_SSH_PASS,
    SRV_ADD_SSH_PORT,
    SRV_ADD_PANEL_ASK,
    SRV_ADD_PANEL_DOMAIN,
    SRV_ADD_PANEL_SCHEME,
    SRV_ADD_PANEL_PORT,
    SRV_ADD_PANEL_PATH,
    SRV_ADD_PANEL_USER,
    SRV_ADD_PANEL_PASS,

    SRV_EDIT_VALUE,

    PORTS_COUNT,
    PORTS_ITEMS,

    MERGE_COUNT,
    MERGE_PORTS,
    MERGE_TARGET,
    MERGE_CONFIRM,

    BK_MENU,
    BK_EXPORT_PICK_SERVER,
    BK_IMPORT_CHOOSE_MODE,
    BK_IMPORT_PICK_SERVER,
    BK_IMPORT_UPLOAD_FILE,
    BK_IMPORT_CONFIRM,
    BK_IMPORT_NEW_SSH_HOST,
    BK_IMPORT_NEW_SSH_USER,
    BK_IMPORT_NEW_SSH_PORT,
    BK_IMPORT_NEW_SSH_PASS,
    BK_IMPORT_NEW_UPLOAD_FILE,
    BK_IMPORT_NEW_CONFIRM,
) = range(31)

# ------------------------- Start / Cancel -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, reply_markup=kb_main(), parse_mode=ParseMode.MARKDOWN)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ عملیات لغو شد 🌙", reply_markup=kb_main())

# ------------------------- Server Manager -------------------------
async def server_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data["new_server"] = {}
    context.user_data["adding_panel_flow"] = False
    await q.edit_message_text("➕ افزودن سرور جدید\n\nلطفاً IPv4 سرور را ارسال کنید:", parse_mode=ParseMode.MARKDOWN)
    return SRV_ADD_IP

async def srv_add_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = (update.message.text or "").strip()
    if not is_ipv4(ip):
        await update.message.reply_text("⚠️ لطفاً یک IPv4 معتبر ارسال کنید.\nمثال: 159.65.243.137")
        return SRV_ADD_IP
    context.user_data["new_server"] = {"ip": ip, "ssh_user": "", "ssh_pass": "", "ssh_port": 22}
    await update.message.reply_text("👤 یوزرنیم SSH را ارسال کنید.\nاگر root است، /skip بزنید 🙂")
    return SRV_ADD_SSH_USER

async def srv_add_ssh_user_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["ssh_user"] = "root"
    await update.message.reply_text("🔑 پسورد SSH را ارسال کنید:")
    return SRV_ADD_SSH_PASS

async def srv_add_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = (update.message.text or "").strip()
    if not u:
        await update.message.reply_text("⚠️ یوزرنیم خالی است. دوباره بفرست 🙂")
        return SRV_ADD_SSH_USER
    context.user_data["new_server"]["ssh_user"] = u
    await update.message.reply_text("🔑 پسورد SSH را ارسال کنید:")
    return SRV_ADD_SSH_PASS

async def srv_add_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = (update.message.text or "").strip()
    if not pw:
        await update.message.reply_text("⚠️ پسورد خالی است. دوباره بفرست 🙂")
        return SRV_ADD_SSH_PASS
    context.user_data["new_server"]["ssh_pass"] = pw
    await update.message.reply_text("🔢 پورت SSH را ارسال کنید.\nپیش‌فرض 22 است؛ اگر 22 هست /skip بزنید 🙂")
    return SRV_ADD_SSH_PORT

async def srv_add_ssh_port_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["ssh_port"] = 22
    await update.message.reply_text("🧩 آیا می‌خواهید پنل XUI هم اضافه شود؟", reply_markup=kb_yes_no_panel())
    return SRV_ADD_PANEL_ASK

async def srv_add_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = validate_port(update.message.text or "")
    if p is None:
        await update.message.reply_text("⚠️ پورت معتبر نیست (1..65535). دوباره بفرست 🙂")
        return SRV_ADD_SSH_PORT
    context.user_data["new_server"]["ssh_port"] = p
    await update.message.reply_text("🧩 آیا می‌خواهید پنل XUI هم اضافه شود؟", reply_markup=kb_yes_no_panel())
    return SRV_ADD_PANEL_ASK

async def srv_add_panel_domain_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = context.user_data["new_server"]["ip"]
    context.user_data["new_server"].setdefault("panel", {})
    context.user_data["new_server"]["panel"]["domain"] = ip
    context.user_data["adding_panel_flow"] = True
    await update.message.reply_text("🔐 نوع دسترسی پنل را انتخاب کنید:", reply_markup=kb_panel_scheme())
    return SRV_ADD_PANEL_SCHEME

async def srv_add_panel_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dom = (update.message.text or "").strip()
    if not dom:
        await update.message.reply_text("⚠️ دامنه خالی است. یا دامنه بده یا /skip بزن 🙂")
        return SRV_ADD_PANEL_DOMAIN
    context.user_data["new_server"].setdefault("panel", {})
    context.user_data["new_server"]["panel"]["domain"] = dom
    context.user_data["adding_panel_flow"] = True
    await update.message.reply_text("🔐 نوع دسترسی پنل را انتخاب کنید:", reply_markup=kb_panel_scheme())
    return SRV_ADD_PANEL_SCHEME

async def srv_add_panel_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = validate_port(update.message.text or "")
    if p is None:
        await update.message.reply_text("⚠️ پورت پنل معتبر نیست (1..65535). دوباره بفرست 🙂")
        return SRV_ADD_PANEL_PORT
    context.user_data["new_server"]["panel"]["panel_port"] = p
    await update.message.reply_text("🧭 Path پنل را ارسال کنید (مثلاً /tracklessvpn/ یا /):")
    return SRV_ADD_PANEL_PATH

async def srv_add_panel_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = (update.message.text or "").strip()
    if not path:
        await update.message.reply_text("⚠️ Path خالی است. دوباره بفرست 🙂")
        return SRV_ADD_PANEL_PATH
    if not path.startswith("/"):
        path = "/" + path
    context.user_data["new_server"]["panel"]["panel_path"] = path
    await update.message.reply_text("👤 یوزرنیم پنل را ارسال کنید:")
    return SRV_ADD_PANEL_USER

async def srv_add_panel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = (update.message.text or "").strip()
    if not u:
        await update.message.reply_text("⚠️ یوزرنیم پنل خالی است. دوباره بفرست 🙂")
        return SRV_ADD_PANEL_USER
    context.user_data["new_server"]["panel"]["panel_user"] = u
    await update.message.reply_text("🔑 پسورد پنل را ارسال کنید:")
    return SRV_ADD_PANEL_PASS

async def srv_add_panel_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = (update.message.text or "").strip()
    if not pw:
        await update.message.reply_text("⚠️ پسورد پنل خالی است. دوباره بفرست 🙂")
        return SRV_ADD_PANEL_PASS
    context.user_data["new_server"]["panel"]["panel_pass"] = pw
    await finalize_add_server(update, context, with_panel=True)
    return ConversationHandler.END

async def finalize_add_server(update: Update, context: ContextTypes.DEFAULT_TYPE, with_panel: bool):
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = context.user_data.get("new_server") or {}

    if not with_panel:
        srv.pop("panel", None)

    ip = srv.get("ip", "")
    sid = safe_server_id(ip)
    base = sid
    i = 2
    while sid in bucket["servers"]:
        sid = f"{base}_{i}"
        i += 1

    srv.setdefault("ports", [])  # user ports (excluding panel port)
    bucket["servers"][sid] = srv
    bucket["order"].append(sid)
    save_store(store)

    lines = [
        "✅ سرور شما اضافه شد 🌿",
        "",
        "🖥 بخش سرور:",
        fmt_kv("Ipv4:", ip),
        fmt_kv("User:", str(srv.get("ssh_user", ""))),
        fmt_kv("Pass:", str(srv.get("ssh_pass", ""))),
        fmt_kv("portssh:", str(srv.get("ssh_port", 22))),
    ]

    if with_panel:
        panel = srv.get("panel") or {}
        url = _fmt_panel_url(panel)
        lines += [
            "",
            "🧩 بخش پنل:",
            fmt_kv("Xui:", url),
            fmt_kv("User:", str(panel.get("panel_user", ""))),
            fmt_kv("Pass:", str(panel.get("panel_pass", ""))),
        ]

    await update.message.reply_text("\n".join(lines), reply_markup=kb_main(), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    context.user_data.clear()

# ------------------------- Ports setup (user input) -------------------------
async def ports_setup_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = q.data.split(":", 1)[1]
    context.user_data["ports_server_id"] = sid
    context.user_data["ports_list"] = []
    await q.edit_message_text("📌 چند تا پورت دارید؟\n(عدد بفرستید، مثلاً 3)")
    return PORTS_COUNT

async def ports_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int((update.message.text or "").strip())
        if not (0 <= n <= 50):
            raise ValueError()
    except Exception:
        await update.message.reply_text("⚠️ عدد معتبر بفرستید (0 تا 50).")
        return PORTS_COUNT

    context.user_data["ports_count"] = n
    context.user_data["ports_list"] = []

    if n == 0:
        await save_ports_and_back(update, context)
        return ConversationHandler.END

    await update.message.reply_text("✅ حالا پورت‌ها را یکی‌یکی ارسال کنید (پورت 1):")
    return PORTS_ITEMS

async def ports_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data.get("ports_count", 0)
    lst: List[int] = context.user_data.get("ports_list", [])
    p = validate_port(update.message.text or "")
    if p is None:
        await update.message.reply_text("⚠️ پورت معتبر نیست (1..65535). دوباره بفرست 🙂")
        return PORTS_ITEMS

    lst.append(p)
    context.user_data["ports_list"] = lst

    idx = len(lst)
    if idx < n:
        await update.message.reply_text(f"✅ پورت {idx} ثبت شد. پورت بعدی (پورت {idx+1}):")
        return PORTS_ITEMS

    await save_ports_and_back(update, context)
    return ConversationHandler.END

async def save_ports_and_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = context.user_data.get("ports_server_id")
    lst: List[int] = context.user_data.get("ports_list", [])
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if srv:
        # ذخیره پورت‌های کاربر (بدون پنل)
        srv["ports"] = lst
        save_store(store)
        await update.message.reply_text("✅ پورت‌ها ذخیره شد 🌟", reply_markup=kb_main())
    else:
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
    context.user_data.pop("ports_server_id", None)
    context.user_data.pop("ports_list", None)
    context.user_data.pop("ports_count", None)

# ------------------------- Server details (SSH/DB find, ports from user) -------------------------
async def show_server_details(update: Update, context: ContextTypes.DEFAULT_TYPE, sid: str):
    q = update.callback_query
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("❌ سرور پیدا نشد.", reply_markup=kb_server_manager(store, update.effective_user.id))
        return

    ip = srv.get("ip", "")
    ssh_user = srv.get("ssh_user", "")
    ssh_pass = srv.get("ssh_pass", "")
    ssh_port = int(srv.get("ssh_port", 22))
    user_ports: List[int] = srv.get("ports") or []

    panel = srv.get("panel") or {}
    has_panel = bool(panel)
    panel_domain = (panel.get("domain") or "").strip()
    panel_url = _fmt_panel_url(panel) if has_panel else ""
    panel_user = (panel.get("panel_user") or "").strip()
    panel_pass = (panel.get("panel_pass") or "").strip()
    panel_port = int(panel.get("panel_port") or 0)

    # SSH/DB check فقط برای پیدا کردن db (طبق خواسته قبلی)
    await q.edit_message_text("⏳ در حال بررسی سرور…", parse_mode=ParseMode.MARKDOWN)
    db_err: Optional[str] = None
    try:
        try:
            code, out, err = await asyncio.wait_for(
                asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, FIND_DB_CMD),
                timeout=45,
            )
        except asyncio.TimeoutError:
            code, out, err = 1, "", "TIMEOUT"

        db_path = (out.strip().splitlines()[-1] if out.strip() else "").strip()
        if code != 0 or (not db_path) or ("NOT_FOUND" in db_path):
            db_err = "خطا: دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم"
    except Exception:
        logger.exception("details ssh/db failed")
        db_err = "خطا: دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم"

    # پورت‌ها از کاربر: همیشه اول پورت پنل، بعد پورت‌های کاربر
    ports_out: List[int] = []
    if panel_port:
        ports_out.append(panel_port)
    for p in user_ports:
        if p not in ports_out:
            ports_out.append(p)

    lines: List[str] = []
    lines.append(fmt_kv("Ipv4:", ip))
    lines.append(fmt_kv("User:", ssh_user))
    lines.append(fmt_kv("Pass:", ssh_pass))
    lines.append("")

    if has_panel:
        lines.append(fmt_kv("Paneldomin:", panel_domain or ip))
        lines.append("")
        lines.append(fmt_kv("Xui:", panel_url))
        lines.append(fmt_kv("User:", panel_user))
        lines.append(fmt_kv("Pass:", panel_pass))
        lines.append("")
        lines.append(fmt_kv("Port panel:", str(panel_port)))
        lines.append("")

    if db_err:
        lines.append(f"`{db_err}`")
        lines.append("")

    lines.append("Port ها خط به خط:")
    for p in ports_out:
        lines.append(f"`{p}`")
    lines.append("")
    lines.append(f"`{','.join(str(x) for x in ports_out)}`")

    await q.edit_message_text(
        "\n".join(lines).strip(),
        reply_markup=kb_server_details_actions(sid),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )

# ------------------------- Edit flow -------------------------
def _server_summary_text(srv: Dict[str, Any]) -> str:
    ip = srv.get("ip", "")
    ssh_user = srv.get("ssh_user", "")
    ssh_pass = srv.get("ssh_pass", "")
    ssh_port = srv.get("ssh_port", 22)
    panel = srv.get("panel") or {}
    has_panel = bool(panel)

    lines = [
        "🧾 خلاصه اطلاعات فعلی",
        "",
        fmt_kv("Ipv4:", ip),
        fmt_kv("User:", ssh_user),
        fmt_kv("Pass:", ssh_pass),
        fmt_kv("portssh:", str(ssh_port)),
    ]
    if has_panel:
        url = _fmt_panel_url(panel)
        lines += [
            "",
            "🧩 پنل XUI",
            fmt_kv("Xui:", url),
            fmt_kv("User:", str(panel.get("panel_user",""))),
            fmt_kv("Pass:", str(panel.get("panel_pass",""))),
        ]
    return "\n".join(lines)

async def show_server_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, sid: str):
    q = update.callback_query
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return
    has_panel = bool(srv.get("panel"))
    text = _server_summary_text(srv) + "\n\n✏️ یکی از گزینه‌های ویرایش را انتخاب کنید:"
    await q.edit_message_text(text, reply_markup=kb_server_edit_menu(sid, has_panel), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def edit_value_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # این Handler همیشه روی متن‌ها فعال است؛ اگر در حالت ویرایش نیستیم باید هیچ کاری نکند
    sid = context.user_data.get("edit_server_id")
    field = context.user_data.get("edit_field")
    if not sid or not field:
        return  # مهم: باعث می‌شود خطای داخلی بی‌دلیل نشان داده نشود

    val = (update.message.text or "").strip()
    if not val:
        await update.message.reply_text("⚠️ مقدار خالی پذیرفته نمی‌شود. دوباره بفرست 🙂")
        return

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        context.user_data.pop("edit_server_id", None)
        context.user_data.pop("edit_field", None)
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return

    try:
        if field == "ip":
            if not is_ipv4(val):
                await update.message.reply_text("⚠️ IPv4 معتبر بفرستید.")
                return
            old_id = sid
            new_ip = val
            new_id = safe_server_id(new_ip)
            base = new_id
            i = 2
            while new_id in bucket["servers"] and new_id != old_id:
                new_id = f"{base}_{i}"
                i += 1

            srv["ip"] = new_ip
            if new_id != old_id:
                bucket["servers"][new_id] = srv
                del bucket["servers"][old_id]
                bucket["order"] = [new_id if x == old_id else x for x in bucket["order"]]
                sid = new_id

        elif field == "ssh_port":
            p = validate_port(val)
            if p is None:
                await update.message.reply_text("⚠️ پورت SSH معتبر نیست (1..65535).")
                return
            srv["ssh_port"] = p

        elif field in ("ssh_user", "ssh_pass"):
            srv[field] = val

        elif field.startswith("panel."):
            srv.setdefault("panel", {})
            key = field.split(".", 1)[1]
            if key == "panel_port":
                p = validate_port(val)
                if p is None:
                    await update.message.reply_text("⚠️ پورت پنل معتبر نیست (1..65535).")
                    return
                srv["panel"][key] = p
            elif key == "panel_path":
                path = val
                if not path.startswith("/"):
                    path = "/" + path
                srv["panel"][key] = path
            else:
                srv["panel"][key] = val
        else:
            srv[field] = val

        save_store(store)
        context.user_data.pop("edit_server_id", None)
        context.user_data.pop("edit_field", None)

        await update.message.reply_text("✅ ویرایش انجام شد 🌟")

        has_panel = bool(srv.get("panel"))
        text = _server_summary_text(srv) + "\n\n✏️ یکی از گزینه‌های ویرایش را انتخاب کنید:"
        await update.message.reply_text(text, reply_markup=kb_server_edit_menu(sid, has_panel), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

    except Exception:
        logger.exception("Edit failed")
        await update.message.reply_text("❌ خطا در ویرایش. دوباره تلاش کنید.", reply_markup=kb_main())
        context.user_data.pop("edit_server_id", None)
        context.user_data.pop("edit_field", None)

# ------------------------- Merge flow (فعال، مثل نسخه قبلی) -------------------------
def inbound_id_by_port_cmd(db_path: str, port: int) -> str:
    return f"""sudo sqlite3 "{db_path}" "SELECT id FROM inbounds WHERE port={port} ORDER BY id DESC LIMIT 1;" """

def make_merge_script() -> str:
    return r"""
set -e
DB="$1"
TARGET_ID="$2"
SRC_IDS="$3"

command -v sqlite3 >/dev/null 2>&1 || { echo "ERR_NO_SQLITE3"; exit 10; }
command -v python3 >/dev/null 2>&1 || { echo "ERR_NO_PYTHON3"; exit 13; }

sudo cp "$DB" "/tmp/xuihub_db_backup_$(date +%s).db" >/dev/null 2>&1 || true

HAS_CLIENTS=$(sudo sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='clients';")
if [ "$HAS_CLIENTS" != "0" ]; then
  COLS=$(sudo sqlite3 "$DB" "SELECT group_concat(name, ',') FROM pragma_table_info('clients') WHERE name NOT IN ('id','inbound_id');")
  if [ -z "$COLS" ]; then echo "ERR_NO_CLIENTS_TABLE"; exit 11; fi

  HAS_UUID=$(sudo sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('clients') WHERE name='uuid';")
  if [ "$HAS_UUID" = "0" ]; then echo "ERR_NO_UUID"; exit 12; fi

  SELS=$(echo "$COLS" | awk -F',' '{for(i=1;i<=NF;i++){printf "c.%s", $i; if(i<NF) printf ","}}')
  BEFORE=$(sudo sqlite3 "$DB" "SELECT COUNT(*) FROM clients WHERE inbound_id=$TARGET_ID;")

  sudo sqlite3 "$DB" "BEGIN;
    INSERT INTO clients (inbound_id, $COLS)
    SELECT $TARGET_ID, $SELS
    FROM clients c
    WHERE c.inbound_id IN ($SRC_IDS)
      AND c.uuid NOT IN (SELECT uuid FROM clients WHERE inbound_id=$TARGET_ID);
    COMMIT;"

  AFTER=$(sudo sqlite3 "$DB" "SELECT COUNT(*) FROM clients WHERE inbound_id=$TARGET_ID;")
  ADDED=$((AFTER-BEFORE))
  echo "OK_MODE=TABLE OK_ADDED=$ADDED BEFORE=$BEFORE AFTER=$AFTER"
  exit 0
fi

python3 - <<'PY' "$DB" "$TARGET_ID" "$SRC_IDS"
import json, sqlite3, sys

db = sys.argv[1]
target_id = int(sys.argv[2])
src_ids = [int(x) for x in sys.argv[3].split(",") if x.strip()]

con = sqlite3.connect(db)
cur = con.cursor()

cur.execute("PRAGMA table_info(inbounds);")
cols = [r[1] for r in cur.fetchall()]
settings_col = None
for cand in ("settings", "setting", "settingsJson", "settings_json"):
    if cand in cols:
        settings_col = cand
        break
if not settings_col:
    print("ERR_NO_SETTINGS_COL")
    sys.exit(20)

def load_settings(inbound_id: int):
    cur.execute(f"SELECT {settings_col} FROM inbounds WHERE id=?", (inbound_id,))
    row = cur.fetchone()
    s = row[0] if row else None
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}

def save_settings(inbound_id: int, obj: dict):
    s = json.dumps(obj, ensure_ascii=False)
    cur.execute(f"UPDATE inbounds SET {settings_col}=? WHERE id=?", (s, inbound_id))

tset = load_settings(target_id)
tclients = tset.get("clients") or []
if not isinstance(tclients, list):
    tclients = []

def client_key(c: dict):
    for k in ("uuid", "id", "email", "password"):
        v = c.get(k)
        if isinstance(v, str) and v.strip():
            return (k, v.strip())
    return ("raw", json.dumps(c, sort_keys=True, ensure_ascii=False))

existing = set()
for c in tclients:
    if isinstance(c, dict):
        existing.add(client_key(c))

added = 0
for sid in src_ids:
    sset = load_settings(sid)
    sclients = sset.get("clients") or []
    if not isinstance(sclients, list):
        continue
    for c in sclients:
        if not isinstance(c, dict):
            continue
        k = client_key(c)
        if k in existing:
            continue
        tclients.append(c)
        existing.add(k)
        added += 1

tset["clients"] = tclients
save_settings(target_id, tset)
con.commit()
con.close()
print(f"OK_MODE=JSON OK_ADDED={added} TARGET_CLIENTS={len(tclients)} SETTINGS_COL={settings_col}")
PY
"""

async def start_merge_pick_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)
    if not bucket["order"]:
        await q.edit_message_text("ابتدا یک سرور اضافه کنید.", reply_markup=kb_server_manager(store, user_id))
        return ConversationHandler.END

    await q.edit_message_text(
        "🔀 مدیریت پورت و کانفیگ\n\nسروری که می‌خواهید ادغام روی آن انجام شود را انتخاب کنید:",
        reply_markup=kb_pick_server_for_action(store, user_id, "merge", "merge"),
    )
    return ConversationHandler.END

async def merge_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = q.data.split(":", 1)[1]

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["merge"] = {"server_id": sid, "ports": []}

    await q.edit_message_text(
        "🔀 ادغام پورت‌ها\n\n"
        "⚠️ پورت مقصد را از قبل داخل پنل ساخته باشید.\n\n"
        "تعداد پورت‌های ورودی را ارسال کنید (مثلاً 2):"
    )
    return MERGE_COUNT

async def merge_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if not (1 <= n <= 30):
            raise ValueError()
    except Exception:
        await update.message.reply_text("⚠️ عدد معتبر (1 تا 30) ارسال کنید.")
        return MERGE_COUNT

    context.user_data["merge"]["count"] = n
    context.user_data["merge"]["ports"] = []
    await update.message.reply_text("✅ حالا پورت‌ها را یکی‌یکی ارسال کنید (پورت 1):")
    return MERGE_PORTS

async def merge_ports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    p = validate_port(update.message.text or "")
    if p is None:
        await update.message.reply_text("⚠️ پورت معتبر ارسال کنید.")
        return MERGE_PORTS

    m["ports"].append(p)
    idx = len(m["ports"])
    if idx < m["count"]:
        await update.message.reply_text(f"✅ پورت {idx} ثبت شد. پورت بعدی (پورت {idx+1}):")
        return MERGE_PORTS

    await update.message.reply_text("✅ همه ورودی‌ها ثبت شد. پورت مقصد را ارسال کنید (مثلاً 443):")
    return MERGE_TARGET

async def merge_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    p = validate_port(update.message.text or "")
    if p is None:
        await update.message.reply_text("⚠️ پورت مقصد معتبر ارسال کنید.")
        return MERGE_TARGET

    m["target_port"] = p
    await update.message.reply_text(
        f"🧾 خلاصه عملیات\n\nورودی‌ها: {m['ports']}\nمقصد: {m['target_port']}\n\nبرای اجرا OK ارسال کنید:"
    )
    return MERGE_CONFIRM

async def merge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "ok":
        await update.message.reply_text("برای ادامه فقط OK ارسال کنید.")
        return MERGE_CONFIRM

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    sid = context.user_data["merge"]["server_id"]
    srv = bucket["servers"].get(sid)
    if not srv:
        context.user_data.clear()
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    ip = srv["ip"]
    ssh_user = srv["ssh_user"]
    ssh_pass = srv["ssh_pass"]
    ssh_port = int(srv.get("ssh_port", 22))
    src_ports = context.user_data["merge"]["ports"]
    target_port = context.user_data["merge"]["target_port"]

    await update.message.reply_text("⏳ در حال اتصال و انجام ادغام...")

    # find db
    try:
        code, out, err = await asyncio.wait_for(
            asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, FIND_DB_CMD),
            timeout=45,
        )
    except asyncio.TimeoutError:
        code, out, err = 1, "", "TIMEOUT"

    db_path = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if code != 0 or (not db_path) or ("NOT_FOUND" in db_path):
        context.user_data.clear()
        await update.message.reply_text("❌ خطا: دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم", reply_markup=kb_main())
        return ConversationHandler.END

    def get_inbound_id(port: int) -> Optional[int]:
        c, o, e = ssh_exec(ip, ssh_port, ssh_user, ssh_pass, inbound_id_by_port_cmd(db_path, port))
        v = (o or "").strip()
        return int(v) if v.isdigit() else None

    target_id = await asyncio.to_thread(get_inbound_id, target_port)
    if not target_id:
        context.user_data.clear()
        await update.message.reply_text(f"❌ inbound مقصد با پورت {target_port} پیدا نشد. اول داخل پنل بساز.", reply_markup=kb_main())
        return ConversationHandler.END

    source_ids = []
    missing = []
    for p in src_ports:
        iid = await asyncio.to_thread(get_inbound_id, p)
        if not iid:
            missing.append(p)
        else:
            source_ids.append(iid)
    if missing:
        context.user_data.clear()
        await update.message.reply_text(f"❌ این پورت‌ها پیدا نشدند: {missing}", reply_markup=kb_main())
        return ConversationHandler.END

    src_ids_csv = ",".join(str(x) for x in source_ids)
    merge_script = make_merge_script()
    remote_cmd = f"""
set -e
TMP=/tmp/xuihub_merge.sh
cat > $TMP <<'EOS'
{merge_script}
EOS
chmod +x $TMP
sudo $TMP "{db_path}" "{target_id}" "{src_ids_csv}"
"""

    try:
        code2, out2, err2 = await asyncio.wait_for(
            asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, remote_cmd),
            timeout=70,
        )
    except asyncio.TimeoutError:
        code2, out2, err2 = 1, "", "TIMEOUT"

    if code2 != 0:
        context.user_data.clear()
        await update.message.reply_text(f"❌ خطا:\n{_short(out2 + '\n' + err2, 3500)}", reply_markup=kb_main())
        return ConversationHandler.END

    await asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, "sudo x-ui restart || sudo systemctl restart x-ui || true")
    context.user_data.clear()
    await update.message.reply_text(f"✅ ادغام انجام شد.\n{out2.strip()}", reply_markup=kb_main())
    return ConversationHandler.END

# ------------------------- Backup (فعال، از نسخه قبل) -------------------------
async def find_db_path(srv: Dict[str, Any]) -> Optional[str]:
    ip = srv["ip"]
    ssh_user = srv["ssh_user"]
    ssh_pass = srv["ssh_pass"]
    ssh_port = int(srv.get("ssh_port", 22))
    code, out, err = await asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, FIND_DB_CMD)
    db_path = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if code != 0 or (not db_path) or ("NOT_FOUND" in db_path):
        return None
    return db_path

async def restart_xui(srv: Dict[str, Any]) -> None:
    ip = srv["ip"]
    ssh_user = srv["ssh_user"]
    ssh_pass = srv["ssh_pass"]
    ssh_port = int(srv.get("ssh_port", 22))
    await asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, "sudo x-ui restart || sudo systemctl restart x-ui || true")

async def backup_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🗂 مدیریت بکاپ\n\n"
        "📤 گرفتن بکاپ: همین لحظه بکاپ کامل دیتابیس را دریافت می‌کنید.\n"
        "📥 وارد کردن بکاپ: بازیابی دیتابیس از فایل بکاپ.\n\n"
        "⚠️ این عملیات از طریق SSH انجام می‌شود.",
        reply_markup=kb_backup_menu(),
    )
    return BK_MENU

async def bk_export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)
    if not bucket["order"]:
        await q.edit_message_text("ابتدا یک سرور اضافه کنید.", reply_markup=kb_server_manager(store, user_id))
        return ConversationHandler.END

    await q.edit_message_text(
        "📤 سرور موردنظر برای بکاپ را انتخاب کنید:",
        reply_markup=kb_pick_server_for_action(store, user_id, "bk_export_server", "bk_export"),
    )
    return BK_EXPORT_PICK_SERVER

async def bk_export_pick_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = q.data.split(":", 1)[1]
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    await q.edit_message_text("⏳ در حال گرفتن بکاپ...")
    db_path = await find_db_path(srv)
    if not db_path:
        await q.edit_message_text("❌ دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم.", reply_markup=kb_main())
        return ConversationHandler.END

    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%d_%H%M")
    remote_tmp = f"/tmp/xuihub_backup_{ts}.db"

    ip = srv["ip"]
    ssh_user = srv["ssh_user"]
    ssh_pass = srv["ssh_pass"]
    ssh_port = int(srv.get("ssh_port", 22))

    remote_cmd = f"""
set -e
sudo cp "{db_path}" "{remote_tmp}"
sudo chmod 644 "{remote_tmp}" || true
echo "{remote_tmp}"
"""
    code, out, err = await asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, remote_cmd)
    if code != 0:
        await q.edit_message_text(f"❌ خطا:\n{_short(out + '\n' + err, 3500)}", reply_markup=kb_main())
        return ConversationHandler.END

    remote_file = out.strip().splitlines()[-1].strip() if out.strip() else remote_tmp
    local_path = None

    try:
        with tempfile.NamedTemporaryFile(prefix="xuihub_backup_", suffix=".db", delete=False) as f:
            local_path = f.name

        def sftp_download():
            c = ssh_client(ip, ssh_port, ssh_user, ssh_pass)
            sftp = c.open_sftp()
            sftp.get(remote_file, local_path)
            sftp.close()
            c.close()

        await asyncio.to_thread(sftp_download)
    except Exception as e:
        await q.edit_message_text(f"❌ خطا در دانلود بکاپ: {e}", reply_markup=kb_main())
        return ConversationHandler.END
    finally:
        await asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, f"sudo rm -f '{remote_file}' || true")

    caption = build_backup_caption(srv.get("ip", sid), now_utc)
    filename = f"xui_backup_{srv.get('ip', sid)}_{ts}.db".replace("/", "_").replace(":", "_")

    try:
        await q.edit_message_text("✅ بکاپ آماده شد. در حال ارسال...")
        await q.message.reply_document(document=InputFile(local_path, filename=filename), caption=caption)
        await q.message.reply_text("✅ انجام شد.", reply_markup=kb_main())
    finally:
        try:
            if local_path and os.path.exists(local_path):
                os.remove(local_path)
        except Exception:
            pass

    return ConversationHandler.END

async def bk_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text("📥 وارد کردن بکاپ\n\nروش بازیابی را انتخاب کنید:", reply_markup=kb_backup_import_mode())
    return BK_IMPORT_CHOOSE_MODE

async def bk_import_existing_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    if not bucket["order"]:
        await q.edit_message_text("ابتدا یک سرور اضافه کنید.", reply_markup=kb_server_manager(store, update.effective_user.id))
        return ConversationHandler.END

    await q.edit_message_text(
        "🔁 سرور مقصد برای Restore را انتخاب کنید:",
        reply_markup=kb_pick_server_for_action(store, update.effective_user.id, "bk_import_server", "bk_import"),
    )
    return BK_IMPORT_PICK_SERVER

async def bk_import_pick_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = q.data.split(":", 1)[1]
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    context.user_data["bk_target_srv"] = srv
    await q.edit_message_text("📎 لطفاً فایل بکاپ دیتابیس را ارسال کنید (فایل .db).\n⚠️ این عملیات دیتابیس فعلی را جایگزین می‌کند.")
    return BK_IMPORT_UPLOAD_FILE

async def bk_import_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("لطفاً فایل بکاپ را به صورت Document ارسال کنید.")
        return BK_IMPORT_UPLOAD_FILE

    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(prefix="xuihub_restore_", suffix=".db", delete=False) as f:
        local_path = f.name
    await tg_file.download_to_drive(custom_path=local_path)

    context.user_data["bk_local_file"] = local_path
    await update.message.reply_text(
        "⚠️ هشدار مهم\n\n"
        "این عملیات دیتابیس فعلی را کامل جایگزین می‌کند.\n"
        "اگر مطمئن هستید، فقط بنویسید: RESTORE"
    )
    return BK_IMPORT_CONFIRM

async def bk_import_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip().lower() != "restore":
        await update.message.reply_text("برای ادامه فقط RESTORE ارسال کنید.")
        return BK_IMPORT_CONFIRM

    srv = context.user_data.get("bk_target_srv")
    local_file = context.user_data.get("bk_local_file")
    if not srv or not local_file or not os.path.exists(local_file):
        context.user_data.clear()
        await update.message.reply_text("❌ فایل یا اطلاعات سرور موجود نیست.", reply_markup=kb_main())
        return ConversationHandler.END

    await update.message.reply_text("⏳ در حال Restore بکاپ...")

    db_path = await find_db_path(srv)
    if not db_path:
        try:
            os.remove(local_file)
        except Exception:
            pass
        context.user_data.clear()
        await update.message.reply_text("❌ دیتابیس پیدا نشد یا sudo ندارم.", reply_markup=kb_main())
        return ConversationHandler.END

    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%d_%H%M")
    remote_upload = f"/tmp/xuihub_restore_upload_{ts}.db"
    remote_backup_old = f"/tmp/xuihub_old_before_restore_{ts}.db"

    ip = srv["ip"]
    ssh_user = srv["ssh_user"]
    ssh_pass = srv["ssh_pass"]
    ssh_port = int(srv.get("ssh_port", 22))

    try:
        def sftp_upload_and_restore():
            c = ssh_client(ip, ssh_port, ssh_user, ssh_pass)
            sftp = c.open_sftp()
            sftp.put(local_file, remote_upload)
            sftp.close()
            cmd = f"""
set -e
sudo cp "{db_path}" "{remote_backup_old}" || true
sudo cp "{remote_upload}" "{db_path}"
sudo chmod 600 "{db_path}" || true
sudo rm -f "{remote_upload}" || true
echo "OK_RESTORE"
"""
            code, out, err = ssh_exec_raw(c, cmd, read_timeout=55)
            c.close()
            return code, out, err

        code, out, err = await asyncio.to_thread(sftp_upload_and_restore)
        if code != 0:
            raise RuntimeError(_short(out + "\n" + err, 3500))

        await restart_xui(srv)
        await update.message.reply_text(f"✅ بکاپ بازیابی شد.\nبکاپ قبلی برای اطمینان ذخیره شد:\n{remote_backup_old}")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در Restore:\n{e}")
    finally:
        try:
            os.remove(local_file)
        except Exception:
            pass
        context.user_data.clear()

    await update.message.reply_text("برای ادامه از منوی اصلی استفاده کنید 👇", reply_markup=kb_main())
    return ConversationHandler.END

# ---- Import new server (no save) ----
async def bk_import_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data["new_ssh"] = {}
    await q.edit_message_text("➕ سرور جدید (بدون ذخیره)\n\n🌐 SSH Host را ارسال کنید:")
    return BK_IMPORT_NEW_SSH_HOST

async def bk_new_ssh_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["host"] = (update.message.text or "").strip()
    await update.message.reply_text("👤 SSH Username را ارسال کنید:")
    return BK_IMPORT_NEW_SSH_USER

async def bk_new_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["user"] = (update.message.text or "").strip()
    await update.message.reply_text("🔢 SSH Port را ارسال کنید (مثلاً 22):")
    return BK_IMPORT_NEW_SSH_PORT

async def bk_new_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = validate_port(update.message.text or "")
    if p is None:
        await update.message.reply_text("⚠️ پورت معتبر ارسال کنید (1..65535).")
        return BK_IMPORT_NEW_SSH_PORT
    context.user_data["new_ssh"]["port"] = p
    await update.message.reply_text("🔑 SSH Password را ارسال کنید:")
    return BK_IMPORT_NEW_SSH_PASS

async def bk_new_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["pass"] = (update.message.text or "").strip()
    await update.message.reply_text("📎 حالا فایل بکاپ دیتابیس .db را ارسال کنید:")
    return BK_IMPORT_NEW_UPLOAD_FILE

async def bk_new_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("فایل را به صورت Document ارسال کنید.")
        return BK_IMPORT_NEW_UPLOAD_FILE
    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(prefix="xuihub_restore_new_", suffix=".db", delete=False) as f:
        local_path = f.name
    await tg_file.download_to_drive(custom_path=local_path)
    context.user_data["bk_local_file"] = local_path
    await update.message.reply_text("⚠️ هشدار\n\nبرای ادامه فقط بنویسید: RESTORE")
    return BK_IMPORT_NEW_CONFIRM

async def bk_new_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip().lower() != "restore":
        await update.message.reply_text("برای ادامه فقط RESTORE ارسال کنید.")
        return BK_IMPORT_NEW_CONFIRM

    ns = context.user_data.get("new_ssh") or {}
    local_file = context.user_data.get("bk_local_file")
    if not ns.get("host") or not ns.get("user") or not ns.get("port") or ns.get("pass") is None:
        context.user_data.clear()
        await update.message.reply_text("❌ اطلاعات SSH کامل نیست.", reply_markup=kb_main())
        return ConversationHandler.END
    if not local_file or not os.path.exists(local_file):
        context.user_data.clear()
        await update.message.reply_text("❌ فایل بکاپ موجود نیست.", reply_markup=kb_main())
        return ConversationHandler.END

    srv = {"ip": ns["host"], "ssh_user": ns["user"], "ssh_port": ns["port"], "ssh_pass": ns["pass"]}
    await update.message.reply_text("⏳ در حال Restore روی سرور جدید...")

    db_path = await find_db_path(srv)
    if not db_path:
        try:
            os.remove(local_file)
        except Exception:
            pass
        context.user_data.clear()
        await update.message.reply_text("❌ دیتابیس پیدا نشد یا sudo ندارم.", reply_markup=kb_main())
        return ConversationHandler.END

    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%d_%H%M")
    remote_upload = f"/tmp/xuihub_restore_upload_{ts}.db"
    remote_backup_old = f"/tmp/xuihub_old_before_restore_{ts}.db"

    try:
        def sftp_upload_and_restore_new():
            c = ssh_client(srv["ip"], int(srv["ssh_port"]), srv["ssh_user"], srv["ssh_pass"])
            sftp = c.open_sftp()
            sftp.put(local_file, remote_upload)
            sftp.close()
            cmd = f"""
set -e
sudo cp "{db_path}" "{remote_backup_old}" || true
sudo cp "{remote_upload}" "{db_path}"
sudo chmod 600 "{db_path}" || true
sudo rm -f "{remote_upload}" || true
echo "OK_RESTORE"
"""
            code, out, err = ssh_exec_raw(c, cmd, read_timeout=55)
            c.close()
            return code, out, err

        code, out, err = await asyncio.to_thread(sftp_upload_and_restore_new)
        if code != 0:
            raise RuntimeError(_short(out + "\n" + err, 3500))

        await restart_xui(srv)
        await update.message.reply_text(
            f"✅ بکاپ بازیابی شد.\nبکاپ قبلی ذخیره شد:\n{remote_backup_old}\n\n"
            "ℹ️ این سرور ذخیره نشد و اطلاعات موقت پاک شد."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در Restore:\n{e}")
    finally:
        try:
            os.remove(local_file)
        except Exception:
            pass
        context.user_data.clear()

    await update.message.reply_text("برای ادامه از منوی اصلی استفاده کنید 👇", reply_markup=kb_main())
    return ConversationHandler.END

# ------------------------- Navigation callbacks -------------------------
async def nav_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    if q.data == "back_main":
        await q.edit_message_text(START_TEXT, reply_markup=kb_main(), parse_mode=ParseMode.MARKDOWN)
        return

    if q.data == "server_manager":
        await q.edit_message_text(
            "🖥 مدیریت سرورها\n\nاز این بخش می‌توانید سرورها را اضافه، مشاهده و مدیریت کنید 🌿",
            reply_markup=kb_server_manager(store, user_id),
        )
        return

    if q.data == "start_merge":
        return await start_merge_pick_server(update, context)

    if q.data == "backup_menu":
        return await backup_menu_entry(update, context)

    if q.data == "profile":
        u = update.effective_user
        username = f"@{u.username}" if u.username else "ندارد"
        servers_count = len(bucket.get("order", []))
        text = (
            "👤 پروفایل شما\n\n"
            f"نام: {u.full_name}\n"
            f"یوزرنیم: {username}\n"
            f"User ID: {u.id}\n\n"
            f"تعداد سرورها: {servers_count}"
        )
        await q.edit_message_text(text, reply_markup=kb_back_main())
        return

    if q.data.startswith("server_details:"):
        sid = q.data.split(":", 1)[1]
        await show_server_details(update, context, sid)
        return

    if q.data.startswith("server_del:"):
        sid = q.data.split(":", 1)[1]
        if sid in bucket["servers"]:
            del bucket["servers"][sid]
            bucket["order"] = [x for x in bucket["order"] if x != sid]
            save_store(store)
        await q.edit_message_text("✅ سرور حذف شد.", reply_markup=kb_server_manager(store, user_id))
        return

    if q.data.startswith("server_edit:"):
        sid = q.data.split(":", 1)[1]
        await show_server_edit_menu(update, context, sid)
        return

    if q.data.startswith("edit_field:"):
        _, sid, field = q.data.split(":", 2)
        context.user_data["edit_server_id"] = sid
        context.user_data["edit_field"] = field
        await q.edit_message_text("✏️ مقدار جدید را ارسال کنید.\nاگر منصرف شدید /cancel بزنید 🙂")
        return

    if q.data.startswith("edit_scheme:"):
        sid = q.data.split(":", 1)[1]
        context.user_data["edit_server_id"] = sid
        context.user_data["edit_field"] = "panel.scheme"
        await q.edit_message_text("🔐 نوع دسترسی پنل را انتخاب کنید:", reply_markup=kb_panel_scheme())
        return

    if q.data.startswith("scheme:"):
        scheme = q.data.split(":", 1)[1].strip().lower()
        if scheme not in ("http", "https"):
            return
        # add flow
        if context.user_data.get("new_server") and context.user_data.get("adding_panel_flow"):
            context.user_data["new_server"]["panel"]["scheme"] = scheme
            await q.edit_message_text("🔢 پورت پنل را ارسال کنید:")
            return SRV_ADD_PANEL_PORT

        # edit flow
        sid = context.user_data.get("edit_server_id")
        field = context.user_data.get("edit_field")
        store2 = load_store()
        bucket2 = get_user_bucket(store2, update.effective_user.id)
        srv = bucket2["servers"].get(sid)
        if srv and field == "panel.scheme":
            srv.setdefault("panel", {})
            srv["panel"]["scheme"] = scheme
            save_store(store2)
            await q.edit_message_text("✅ انجام شد 🌟")
            await show_server_edit_menu(update, context, sid)
        return

    if q.data == "add_panel_yes":
        await q.edit_message_text("🌐 دامنه پنل را ارسال کنید.\nاگر دامنه ندارید /skip بزنید 🙂")
        return SRV_ADD_PANEL_DOMAIN

    if q.data == "add_panel_no":
        # finalize without panel
        await finalize_add_server(update, context, with_panel=False)
        return ConversationHandler.END

    if q.data.startswith("ports_setup:"):
        return await ports_setup_entry(update, context)

    # Backup routes
    if q.data == "bk_export":
        return await bk_export_start(update, context)
    if q.data.startswith("bk_export_server:"):
        return await bk_export_pick_server(update, context)
    if q.data == "bk_import":
        return await bk_import_start(update, context)
    if q.data == "bk_import_existing":
        return await bk_import_existing_choose(update, context)
    if q.data.startswith("bk_import_server:"):
        return await bk_import_pick_server(update, context)
    if q.data == "bk_import_new":
        return await bk_import_new_start(update, context)

# ------------------------- Global error handler (no crash) -------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)
    # اینجا دیگه "یک خطای داخلی..." بی‌دلیل اسپم نمی‌کنیم.
    # فقط اگر کاربر در یک عملیات بوده، یک پیام کوتاه می‌دهیم.
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ یک مشکل رخ داد. دوباره تلاش کنید یا /start بزنید.",
                reply_markup=kb_main(),
            )
    except Exception:
        pass

# ------------------------- Main -------------------------
def main():
    token = env_required("TOKEN")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # ---- Add Server Conversation ----
    conv_add_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(server_add_entry, pattern=r"^server_add$")],
        states={
            SRV_ADD_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ip)],
            SRV_ADD_SSH_USER: [
                CommandHandler("skip", srv_add_ssh_user_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ssh_user),
            ],
            SRV_ADD_SSH_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ssh_pass)],
            SRV_ADD_SSH_PORT: [
                CommandHandler("skip", srv_add_ssh_port_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ssh_port),
            ],
            SRV_ADD_PANEL_ASK: [CallbackQueryHandler(nav_callbacks, pattern=r"^(add_panel_yes|add_panel_no)$")],
            SRV_ADD_PANEL_DOMAIN: [
                CommandHandler("skip", srv_add_panel_domain_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_domain),
            ],
            SRV_ADD_PANEL_SCHEME: [CallbackQueryHandler(nav_callbacks, pattern=r"^scheme:(http|https)$")],
            SRV_ADD_PANEL_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_port)],
            SRV_ADD_PANEL_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_path)],
            SRV_ADD_PANEL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_user)],
            SRV_ADD_PANEL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_pass)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_add_server)

    # ---- Ports setup conversation ----
    conv_ports = ConversationHandler(
        entry_points=[CallbackQueryHandler(ports_setup_entry, pattern=r"^ports_setup:")],
        states={
            PORTS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ports_count)],
            PORTS_ITEMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ports_items)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_ports)

    # ---- Merge conversation ----
    conv_merge = ConversationHandler(
        entry_points=[CallbackQueryHandler(merge_entry, pattern=r"^merge:")],
        states={
            MERGE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, merge_count)],
            MERGE_PORTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, merge_ports)],
            MERGE_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, merge_target)],
            MERGE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, merge_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_merge)

    # ---- Backup conversation ----
    conv_backup = ConversationHandler(
        entry_points=[CallbackQueryHandler(backup_menu_entry, pattern=r"^backup_menu$")],
        states={
            BK_MENU: [CallbackQueryHandler(nav_callbacks)],
            BK_EXPORT_PICK_SERVER: [CallbackQueryHandler(nav_callbacks)],
            BK_IMPORT_CHOOSE_MODE: [CallbackQueryHandler(nav_callbacks)],
            BK_IMPORT_PICK_SERVER: [CallbackQueryHandler(nav_callbacks)],
            BK_IMPORT_UPLOAD_FILE: [MessageHandler(filters.Document.ALL, bk_import_receive_file)],
            BK_IMPORT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_import_confirm)],
            BK_IMPORT_NEW_SSH_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_ssh_host)],
            BK_IMPORT_NEW_SSH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_ssh_user)],
            BK_IMPORT_NEW_SSH_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_ssh_port)],
            BK_IMPORT_NEW_SSH_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_ssh_pass)],
            BK_IMPORT_NEW_UPLOAD_FILE: [MessageHandler(filters.Document.ALL, bk_new_receive_file)],
            BK_IMPORT_NEW_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_backup)

    # ---- Edit value message handler (safe guard inside) ----
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_message))

    # ---- Navigation LAST ----
    app.add_handler(CallbackQueryHandler(nav_callbacks))

    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
