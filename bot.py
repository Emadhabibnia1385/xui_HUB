# bot.py
# Compatible with python-telegram-bot 21.6
# هدف: 🖥 مدیریت سرورها + (اختیاری) افزودن پنل XUI
# نکته: در Start هیچ SSH/DB انجام نمی‌شود. فقط در جزئیات سرور/ادغام/بکاپ.

import os
import json
import re
import asyncio
import logging
from typing import Dict, Any, Optional, Tuple, List

import paramiko
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("xui_hub")

STORE_FILE = "store.json"
ENV_FALLBACK_PATH = "/opt/xui_HUB/.env"

# =========================
# Robust SSH/DB commands
# =========================
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

PORTS_QUERY = r"""sudo sqlite3 "{db}" "SELECT port FROM inbounds ORDER BY port ASC;" """

# =========================
# Storage
# =========================
def load_store() -> Dict[str, Any]:
    if not os.path.exists(STORE_FILE):
        return {"users": {}}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # اگر فایل خراب شد، ربات کرش نکند
        return {"users": {}}


def save_store(data: Dict[str, Any]) -> None:
    try:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to save store.json")


def get_user_bucket(store: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    uid = str(user_id)
    store.setdefault("users", {})
    store["users"].setdefault(uid, {"servers": {}, "order": []})
    bucket = store["users"][uid]
    bucket.setdefault("servers", {})
    bucket.setdefault("order", [])
    return bucket


def safe_server_id(ip: str) -> str:
    sid = re.sub(r"[^0-9.]+", "", ip.strip())
    return sid or re.sub(r"[^a-zA-Z0-9_.-]+", "_", ip.strip()) or "server"


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


# =========================
# ENV loader (TOKEN)
# =========================
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
        logger.exception("Failed to read .env")
    return out


def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if v:
        return v
    # fallback to /opt/xui_HUB/.env
    envs = load_env_file(ENV_FALLBACK_PATH)
    v2 = (envs.get(name) or "").strip()
    if v2:
        return v2
    raise RuntimeError(f"Missing env: {name}")


# =========================
# SSH helpers (safe + timeouts)
# =========================
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


# =========================
# UI / Keyboards
# =========================
START_TEXT = (
    "🤖 **به xui_HUB خوش آمدید**\n\n"
    "این ربات، همراهِ آرامِ شماست برای **مدیریت سرورها** و (در صورت تمایل) **پنل‌های XUI**.\n"
    "از منوی زیر، مقصدتان را انتخاب کنید 👇\n\n"
    "👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖥 مدیریت سرورها", callback_data="server_manager")],
            # فعلاً دست‌نخورده (اما می‌توانید بعداً فعالش کنید)
            [InlineKeyboardButton("🔀 مدیریت پورت و کانفیگ", callback_data="start_merge")],
            [InlineKeyboardButton("🗂 مدیریت بکاپ", callback_data="backup_menu")],
            [InlineKeyboardButton("👤 پروفایل", callback_data="profile")],
        ]
    )

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])

def kb_yes_no_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ تایید", callback_data="add_panel_yes"),
            InlineKeyboardButton("❌ خیر", callback_data="add_panel_no"),
        ]]
    )

def kb_panel_scheme() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🔒 HTTP", callback_data="scheme:http"),
            InlineKeyboardButton("🔐 HTTPS", callback_data="scheme:https"),
        ]]
    )

def kb_server_details_actions(server_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✏️ ویرایش اطلاعات", callback_data=f"server_edit:{server_id}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"server_del:{server_id}"),
        ],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="server_manager")]]
    )

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
    if dom:
        return f"{ip} ({dom})"
    return ip


def kb_server_manager(store: Dict[str, Any], user_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(store, user_id)
    rows: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ افزودن سرور", callback_data="server_add")]
    ]
    for sid in bucket.get("order", []):
        s = bucket["servers"].get(sid)
        if not s:
            continue
        rows.append([InlineKeyboardButton(_panel_button_label(s), callback_data=f"server_details:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


# =========================
# States
# =========================
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
) = range(12)


# =========================
# Commands
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # هیچ اتصال SSH/DB اینجا انجام نمی‌شود
    await update.message.reply_text(START_TEXT, reply_markup=kb_main(), parse_mode=ParseMode.MARKDOWN)


# =========================
# Navigation callbacks (only non-conversation items)
# =========================
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
        text = "🖥 **مدیریت سرورها**\n\nاز اینجا می‌توانید سرورهای خود را با آرامش مدیریت کنید 🌿"
        await q.edit_message_text(text, reply_markup=kb_server_manager(store, user_id), parse_mode=ParseMode.MARKDOWN)
        return

    # فعلاً دست نخورده، اما برای جلوگیری از باگ/سکوت، پیام می‌دهیم
    if q.data == "start_merge":
        await q.edit_message_text(
            "🔀 این بخش فعلاً در حال تکمیل است.\n\n"
            "به‌زودی با قدرتِ بیشتر برمی‌گردد ✨",
            reply_markup=kb_back_main(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if q.data == "backup_menu":
        await q.edit_message_text(
            "🗂 این بخش فعلاً در حال تکمیل است.\n\n"
            "فعلاً از مدیریت سرورها استفاده کنید 🌸",
            reply_markup=kb_back_main(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if q.data == "profile":
        u = update.effective_user
        username = f"@{u.username}" if u.username else "ندارد"
        servers_count = len(bucket.get("order", []))
        text = (
            "👤 **پروفایل شما**\n\n"
            f"نام: {u.full_name}\n"
            f"یوزرنیم: {username}\n"
            f"User ID: {u.id}\n\n"
            f"تعداد سرورها: {servers_count}"
        )
        await q.edit_message_text(text, reply_markup=kb_back_main(), parse_mode=ParseMode.MARKDOWN)
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
        await q.edit_message_text("✅ سرور با لطافت حذف شد 🌙", reply_markup=kb_server_manager(store, user_id))
        return

    if q.data.startswith("server_edit:"):
        sid = q.data.split(":", 1)[1]
        await show_server_edit_menu(update, context, sid)
        return

    if q.data.startswith("edit_field:"):
        _, sid, field = q.data.split(":", 2)
        context.user_data["edit_server_id"] = sid
        context.user_data["edit_field"] = field
        await q.edit_message_text(
            "✏️ **ویرایش**\n\n"
            "لطفاً مقدار جدید را ارسال کنید.\n"
            "اگر منصرف شدید، فقط /cancel بزنید 🙂",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if q.data.startswith("edit_scheme:"):
        sid = q.data.split(":", 1)[1]
        context.user_data["edit_server_id"] = sid
        context.user_data["edit_field"] = "panel.scheme"
        await q.edit_message_text(
            "🔐 **انتخاب نوع دسترسی پنل**\n\n"
            "یکی را انتخاب کنید:",
            reply_markup=kb_panel_scheme(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if q.data.startswith("scheme:"):
        # هم برای افزودن پنل، هم برای ویرایش scheme
        scheme = q.data.split(":", 1)[1].strip().lower()
        if scheme not in ("http", "https"):
            return

        # اگر در افزودن پنل هستیم
        if context.user_data.get("new_server") and context.user_data.get("adding_panel_flow"):
            context.user_data["new_server"]["panel"]["scheme"] = scheme
            await q.edit_message_text("🔢 حالا **پورت پنل** را ارسال کنید:", parse_mode=ParseMode.MARKDOWN)
            return SRV_ADD_PANEL_PORT

        # اگر در ویرایش هستیم
        sid = context.user_data.get("edit_server_id")
        field = context.user_data.get("edit_field")
        store2 = load_store()
        bucket2 = get_user_bucket(store2, update.effective_user.id)
        srv = bucket2["servers"].get(sid)
        if srv and field == "panel.scheme":
            srv.setdefault("panel", {})
            srv["panel"]["scheme"] = scheme
            save_store(store2)
            await q.edit_message_text("✅ نوع دسترسی پنل ویرایش شد 🌟", parse_mode=ParseMode.MARKDOWN)
            await show_server_edit_menu(update, context, sid)
        return

    if q.data == "add_panel_yes":
        # ادامه‌ی افزودن پنل
        await q.edit_message_text(
            "🌐 **دامنه پنل** را ارسال کنید.\n"
            "اگر دامنه ندارید، /skip بزنید تا همان IP ثبت شود 🙂",
            parse_mode=ParseMode.MARKDOWN,
        )
        return SRV_ADD_PANEL_DOMAIN

    if q.data == "add_panel_no":
        # ذخیره فقط سرور
        await finalize_add_server(update, context, with_panel=False)
        return ConversationHandler.END


# =========================
# Server Details (SSH + DB only here)
# =========================
def _fmt_panel_url(panel: Dict[str, Any]) -> str:
    scheme = (panel.get("scheme") or "http").strip()
    dom = (panel.get("domain") or "").strip()
    pport = panel.get("panel_port")
    ppath = (panel.get("panel_path") or "/").strip()
    if not ppath.startswith("/"):
        ppath = "/" + ppath
    if not dom:
        dom = "0.0.0.0"
    if not pport:
        pport = 0
    return f"{scheme}://{dom}:{pport}{ppath}"


async def show_server_details(update: Update, context: ContextTypes.DEFAULT_TYPE, server_id: str):
    q = update.callback_query
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(server_id)
    if not srv:
        await q.edit_message_text("❌ سرور پیدا نشد.", reply_markup=kb_server_manager(store, update.effective_user.id))
        return

    ip = srv.get("ip", "")
    ssh_user = srv.get("ssh_user", "")
    ssh_pass = srv.get("ssh_pass", "")
    ssh_port = int(srv.get("ssh_port", 22))

    panel = srv.get("panel") or {}
    panel_domain = (panel.get("domain") or "").strip()
    panel_url = _fmt_panel_url(panel) if panel else ""
    panel_user = (panel.get("panel_user") or "").strip()
    panel_pass = (panel.get("panel_pass") or "").strip()
    panel_port = panel.get("panel_port")

    # در همینجا SSH/DB انجام می‌شود
    await q.edit_message_text("⏳ کمی صبر… با سرور نجوا می‌کنم تا پورت‌ها را بیاورد 🌙", parse_mode=ParseMode.MARKDOWN)

    ports: List[int] = []
    db_err: Optional[str] = None
    try:
        # 1) find db
        try:
            code, out, err = await asyncio.wait_for(
                asyncio.to_thread(
                    ssh_exec, ip, ssh_port, ssh_user, ssh_pass, FIND_DB_CMD
                ),
                timeout=45,
            )
        except asyncio.TimeoutError:
            db_err = "خطا: دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم"
            code, out, err = 1, "", "TIMEOUT"

        db_path = (out.strip().splitlines()[-1] if out.strip() else "").strip()

        if code != 0 or (not db_path) or ("NOT_FOUND" in db_path):
            db_err = "خطا: دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم"
        else:
            # 2) get ports
            cmd = PORTS_QUERY.format(db=db_path)
            try:
                code2, out2, err2 = await asyncio.wait_for(
                    asyncio.to_thread(
                        ssh_exec, ip, ssh_port, ssh_user, ssh_pass, cmd
                    ),
                    timeout=45,
                )
            except asyncio.TimeoutError:
                code2, out2, err2 = 1, "", "TIMEOUT"

            if code2 != 0:
                db_err = "خطا: دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم"
            else:
                # out2 lines of ports
                for line in out2.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        ports.append(int(line))
                ports = sorted(set(ports))

    except Exception:
        # هیچ‌وقت کرش نکند
        logger.exception("Server details failed")
        db_err = "خطا: دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم"

    # build exact output template (copyable in backticks)
    lines: List[str] = []
    lines.append(f"`Ipv4: {ip}`")
    lines.append(f"`User: {ssh_user}`")
    lines.append(f"`Pass: {ssh_pass}`")
    lines.append("")
    if panel_domain:
        lines.append(f"`Paneldomin: {panel_domain}`")
        lines.append("")
        lines.append(f"`Xui: {panel_url}`")
        lines.append(f"`User: {panel_user}`")
        lines.append(f"`Pass: {panel_pass}`")
        lines.append("")
        if panel_port:
            lines.append(f"`Port panel: {panel_port}`")
        else:
            lines.append("`Port panel: 0`")
        lines.append("")

    if db_err:
        lines.append(f"`{db_err}`")
    else:
        lines.append("Port ها خط به خط:")
        for p in ports:
            lines.append(f"`{p}`")
        lines.append("")
        csv = ",".join(str(p) for p in ports)
        lines.append(f"`{csv}`")

    text = "\n".join(lines).strip()
    await q.edit_message_text(
        text,
        reply_markup=kb_server_details_actions(server_id),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


# =========================
# Edit Menu
# =========================
def _server_summary_text(srv: Dict[str, Any]) -> str:
    ip = srv.get("ip", "")
    ssh_user = srv.get("ssh_user", "")
    ssh_pass = srv.get("ssh_pass", "")
    ssh_port = srv.get("ssh_port", 22)

    panel = srv.get("panel") or {}
    has_panel = bool(panel)
    lines = [
        "🧾 **خلاصه اطلاعات فعلی**",
        "",
        f"`Ipv4: {ip}`",
        f"`User: {ssh_user}`",
        f"`Pass: {ssh_pass}`",
        f"`portssh:{ssh_port}`",
    ]
    if has_panel:
        url = _fmt_panel_url(panel)
        lines += [
            "",
            "🧩 **پنل XUI**",
            f"`Xui: {url}`",
            f"`User: {panel.get('panel_user','')}`",
            f"`Pass: {panel.get('panel_pass','')}`",
        ]
    return "\n".join(lines)

async def show_server_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, server_id: str):
    q = update.callback_query
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(server_id)
    if not srv:
        await q.edit_message_text("❌ سرور پیدا نشد.", reply_markup=kb_server_manager(store, update.effective_user.id))
        return
    has_panel = bool(srv.get("panel"))
    text = _server_summary_text(srv) + "\n\n" + "✏️ **یکی از گزینه‌های ویرایش را انتخاب کنید:**"
    await q.edit_message_text(
        text,
        reply_markup=kb_server_edit_menu(server_id, has_panel),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def edit_value_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # کاربر مقدار جدید را می‌فرستد
    sid = context.user_data.get("edit_server_id")
    field = context.user_data.get("edit_field")
    if not sid or not field:
        return ConversationHandler.END

    val = (update.message.text or "").strip()
    if not val:
        await update.message.reply_text("⚠️ مقدار خالی پذیرفته نمی‌شود. دوباره بفرست 🙂")
        return SRV_EDIT_VALUE

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        context.user_data.pop("edit_server_id", None)
        context.user_data.pop("edit_field", None)
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    # Validation + set
    try:
        if field == "ip":
            if not is_ipv4(val):
                await update.message.reply_text("⚠️ لطفاً یک IPv4 معتبر ارسال کنید (مثلاً 159.65.243.137).")
                return SRV_EDIT_VALUE
            # اگر ID مبتنی بر IP است، باید کلید را هم تغییر دهیم
            old_id = sid
            new_ip = val
            new_id = safe_server_id(new_ip)
            # جلوگیری از تداخل
            base = new_id
            i = 2
            while new_id in bucket["servers"] and new_id != old_id:
                new_id = f"{base}_{i}"
                i += 1

            srv["ip"] = new_ip
            if new_id != old_id:
                # move
                bucket["servers"][new_id] = srv
                del bucket["servers"][old_id]
                bucket["order"] = [new_id if x == old_id else x for x in bucket["order"]]
                sid = new_id  # برای برگشت به منو

        elif field == "ssh_port":
            p = validate_port(val)
            if p is None:
                await update.message.reply_text("⚠️ پورت SSH معتبر نیست (1..65535).")
                return SRV_EDIT_VALUE
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
                    return SRV_EDIT_VALUE
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

        # پاکسازی
        context.user_data.pop("edit_server_id", None)
        context.user_data.pop("edit_field", None)

        await update.message.reply_text("✅ ویرایش انجام شد 🌟")
        # بازگشت به منوی ویرایش
        dummy_update = update  # we don't have callback; send a new message with menu
        has_panel = bool(srv.get("panel"))
        text = _server_summary_text(srv) + "\n\n" + "✏️ **یکی از گزینه‌های ویرایش را انتخاب کنید:**"
        await dummy_update.message.reply_text(
            text,
            reply_markup=kb_server_edit_menu(sid, has_panel),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        return ConversationHandler.END

    except Exception:
        logger.exception("Edit failed")
        await update.message.reply_text("❌ خطایی رخ داد. دوباره تلاش کنید.")
        return ConversationHandler.END


# =========================
# Add Server Conversation
# =========================
async def server_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data["new_server"] = {}
    context.user_data["adding_panel_flow"] = False

    await q.edit_message_text(
        "➕ **افزودن سرور جدید**\n\n"
        "لطفاً **IPv4 سرور** را ارسال کنید 🌿\n"
        "مثال: `159.65.243.137`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return SRV_ADD_IP


async def srv_add_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = (update.message.text or "").strip()
    if not is_ipv4(ip):
        await update.message.reply_text("⚠️ لطفاً یک IPv4 معتبر ارسال کنید (مثلاً 159.65.243.137).")
        return SRV_ADD_IP

    context.user_data["new_server"] = {
        "ip": ip,
        "ssh_user": "",
        "ssh_pass": "",
        "ssh_port": 22,
    }

    await update.message.reply_text(
        "👤 **یوزرنیم SSH** را ارسال کنید.\n"
        "اگر `root` است، می‌توانید `/skip` بزنید 🙂",
        parse_mode=ParseMode.MARKDOWN,
    )
    return SRV_ADD_SSH_USER


async def srv_add_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = (update.message.text or "").strip()
    if not user:
        await update.message.reply_text("⚠️ یوزرنیم نمی‌تواند خالی باشد.")
        return SRV_ADD_SSH_USER
    context.user_data["new_server"]["ssh_user"] = user
    await update.message.reply_text("🔑 **پسورد SSH** را ارسال کنید:")
    return SRV_ADD_SSH_PASS


async def srv_add_ssh_user_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["ssh_user"] = "root"
    await update.message.reply_text("🔑 **پسورد SSH** را ارسال کنید:")
    return SRV_ADD_SSH_PASS


async def srv_add_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = (update.message.text or "").strip()
    if not pw:
        await update.message.reply_text("⚠️ پسورد نمی‌تواند خالی باشد.")
        return SRV_ADD_SSH_PASS
    context.user_data["new_server"]["ssh_pass"] = pw
    await update.message.reply_text(
        "🔢 **پورت SSH** را ارسال کنید.\n"
        "پیش‌فرض `22` است؛ اگر همان 22 است، `/skip` بزنید 🙂",
        parse_mode=ParseMode.MARKDOWN,
    )
    return SRV_ADD_SSH_PORT


async def srv_add_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = validate_port(update.message.text or "")
    if p is None:
        await update.message.reply_text("⚠️ پورت معتبر نیست (1..65535). دوباره بفرست 🙂")
        return SRV_ADD_SSH_PORT
    context.user_data["new_server"]["ssh_port"] = p

    await update.message.reply_text(
        "🧩 آیا دوست دارید **پنل XUI** هم برای این سرور ثبت شود؟\n\n"
        "اگر فعلاً نمی‌خواهید، هیچ اشکالی ندارد 🌸",
        reply_markup=kb_yes_no_panel(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return SRV_ADD_PANEL_ASK


async def srv_add_ssh_port_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["ssh_port"] = 22
    await update.message.reply_text(
        "🧩 آیا دوست دارید **پنل XUI** هم برای این سرور ثبت شود؟\n\n"
        "اگر فعلاً نمی‌خواهید، هیچ اشکالی ندارد 🌸",
        reply_markup=kb_yes_no_panel(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return SRV_ADD_PANEL_ASK


async def srv_add_panel_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dom = (update.message.text or "").strip()
    if not dom:
        await update.message.reply_text("⚠️ دامنه خالی است. یا دامنه بده یا /skip بزن 🙂")
        return SRV_ADD_PANEL_DOMAIN

    context.user_data["new_server"].setdefault("panel", {})
    context.user_data["new_server"]["panel"]["domain"] = dom
    context.user_data["adding_panel_flow"] = True

    await update.message.reply_text(
        "🔐 حالا **نوع دسترسی پنل** را انتخاب کنید:",
        reply_markup=kb_panel_scheme(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return SRV_ADD_PANEL_SCHEME


async def srv_add_panel_domain_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = context.user_data["new_server"]["ip"]
    context.user_data["new_server"].setdefault("panel", {})
    context.user_data["new_server"]["panel"]["domain"] = ip
    context.user_data["adding_panel_flow"] = True

    await update.message.reply_text(
        "🔐 حالا **نوع دسترسی پنل** را انتخاب کنید:",
        reply_markup=kb_panel_scheme(),
        parse_mode=ParseMode.MARKDOWN,
    )
    return SRV_ADD_PANEL_SCHEME


async def srv_add_panel_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = validate_port(update.message.text or "")
    if p is None:
        await update.message.reply_text("⚠️ پورت پنل معتبر نیست (1..65535). دوباره بفرست 🙂")
        return SRV_ADD_PANEL_PORT
    context.user_data["new_server"]["panel"]["panel_port"] = p
    await update.message.reply_text("🧭 **Path پنل** را ارسال کنید (مثلاً `/tracklessvpn/` یا `/`) :")
    return SRV_ADD_PANEL_PATH


async def srv_add_panel_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = (update.message.text or "").strip()
    if not path:
        await update.message.reply_text("⚠️ Path خالی است. دوباره بفرست 🙂")
        return SRV_ADD_PANEL_PATH
    if not path.startswith("/"):
        path = "/" + path
    context.user_data["new_server"]["panel"]["panel_path"] = path
    await update.message.reply_text("👤 **Username پنل** را ارسال کنید:")
    return SRV_ADD_PANEL_USER


async def srv_add_panel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = (update.message.text or "").strip()
    if not u:
        await update.message.reply_text("⚠️ یوزرنیم پنل خالی است. دوباره بفرست 🙂")
        return SRV_ADD_PANEL_USER
    context.user_data["new_server"]["panel"]["panel_user"] = u
    await update.message.reply_text("🔑 **Password پنل** را ارسال کنید:")
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
    ip = srv.get("ip", "")
    ssh_user = srv.get("ssh_user", "")
    ssh_pass = srv.get("ssh_pass", "")
    ssh_port = srv.get("ssh_port", 22)

    # اگر پنل نخواست، پاکش کن
    if not with_panel:
        srv.pop("panel", None)

    sid = safe_server_id(ip)
    base = sid
    i = 2
    while sid in bucket["servers"]:
        sid = f"{base}_{i}"
        i += 1

    bucket["servers"][sid] = srv
    bucket["order"].append(sid)
    save_store(store)

    # پیام نهایی ادبی + قابل کپی
    lines = [
        "✅ **سرور شما با عشق ثبت شد** 🌿",
        "",
        "🖥 **بخش سرور:**",
        f"`Ipv4: {ip}`",
        f"`User: {ssh_user}`",
        f"`Pass: {ssh_pass}`",
        f"`portssh:{ssh_port}`",
    ]

    if with_panel:
        panel = srv.get("panel") or {}
        url = _fmt_panel_url(panel)
        dom = panel.get("domain", "")
        lines += [
            "",
            "🧩 **بخش پنل:**",
            f"`Xui: {url}`",
            f"`User: {panel.get('panel_user','')}`",
            f"`Pass: {panel.get('panel_pass','')}`",
        ]
        # اگر دامنه جداست، نمایش بده
        if dom:
            lines.insert(lines.index("🧩 **بخش پنل:**"), f"`Paneldomin: {dom}`")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
    context.user_data.clear()


# =========================
# Cancel
# =========================
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ لغو شد. هر وقت خواستی، دوباره از نو 🌙", reply_markup=kb_main())


# =========================
# Error handler (never crash)
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)
    try:
        if isinstance(update, Update):
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ یک خطای داخلی رخ داد، اما ربات زنده است 🙂\n"
                    "دوباره تلاش کنید یا /start بزنید.",
                    reply_markup=kb_main(),
                )
    except Exception:
        pass


# =========================
# main()
# =========================
def main():
    token = env_required("TOKEN")
    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # -------------------------
    # Conversations FIRST
    # -------------------------
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
            # این state با دکمه‌ها مدیریت می‌شود (add_panel_yes/no)
            SRV_ADD_PANEL_ASK: [CallbackQueryHandler(nav_callbacks, pattern=r"^(add_panel_yes|add_panel_no)$")],
            SRV_ADD_PANEL_DOMAIN: [
                CommandHandler("skip", srv_add_panel_domain_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_domain),
            ],
            SRV_ADD_PANEL_SCHEME: [
                CallbackQueryHandler(nav_callbacks, pattern=r"^scheme:(http|https)$")
            ],
            SRV_ADD_PANEL_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_port)],
            SRV_ADD_PANEL_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_path)],
            SRV_ADD_PANEL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_user)],
            SRV_ADD_PANEL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_pass)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_add_server)

    # Edit value conversation (only after clicking edit_field)
    conv_edit_value = ConversationHandler(
        entry_points=[
            MessageHandler(filters.ALL & filters.Regex(r"^$") , edit_value_message)  # dummy; we trigger by user_data
        ],
        states={
            SRV_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_message)]
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    # نکته: ما edit_value_message را مستقیم با MessageHandler هم می‌گیریم
    # تا وقتی edit_field انتخاب شد، پیام بعدی کاربر ذخیره شود.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_message))

    # -------------------------
    # Navigation LAST (pattern دقیق)
    # -------------------------
    app.add_handler(
        CallbackQueryHandler(
            nav_callbacks,
            pattern=r"^(back_main|server_manager|start_merge|backup_menu|profile|server_details:.*|server_del:.*|server_edit:.*|edit_field:.*|edit_scheme:.*|scheme:(http|https)|add_panel_yes|add_panel_no)$",
        )
    )

    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
