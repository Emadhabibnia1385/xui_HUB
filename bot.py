
import os
import json
import re
import asyncio
import tempfile
from enum import IntEnum, auto
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List, Set

import paramiko
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

STORE_FILE = "store.json"
SKIP_CMD = "/skip"

# ---------------- Storage ----------------
def load_store() -> Dict[str, Any]:
    if not os.path.exists(STORE_FILE):
        return {"users": {}}
    with open(STORE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_store(data: Dict[str, Any]) -> None:
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_user_bucket(store: Dict[str, Any], user_id: int) -> Dict[str, Any]:
    uid = str(user_id)
    store.setdefault("users", {})
    store["users"].setdefault(uid, {"servers": {}, "order": []})
    return store["users"][uid]

def safe_id(host: str) -> str:
    x = re.sub(r"[^a-zA-Z0-9_.-]+", "_", host.strip())
    return x or "server"

def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v

def is_skip(text: str) -> bool:
    return text.strip().lower() == SKIP_CMD

def is_real_command(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("/") and t not in ("/skip",)

# ---------------- Jalali (Shamsi) ----------------
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

# ---------------- SSH helpers ----------------
def ssh_client(host: str, port: int, user: str, password: str, timeout: int = 25) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=host, port=port, username=user, password=password, timeout=timeout)
    return c

def ssh_exec_raw(c: paramiko.SSHClient, cmd: str) -> Tuple[int, str, str]:
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    code = stdout.channel.recv_exit_status()
    return code, out, err

def ssh_exec(host: str, port: int, user: str, password: str, cmd: str, timeout: int = 25) -> Tuple[int, str, str]:
    c = ssh_client(host, port, user, password, timeout=timeout)
    code, out, err = ssh_exec_raw(c, cmd)
    c.close()
    return code, out, err

async def ssh_run_cmd(ssh: Dict[str, Any], cmd: str) -> Tuple[int, str, str]:
    return await asyncio.to_thread(
        ssh_exec,
        ssh["ssh_host"], ssh["ssh_port"], ssh["ssh_user"], ssh["ssh_pass"],
        cmd
    )

# ---------------- x-ui DB helpers ----------------
FIND_DB_CMD = r"""
set -e
DB=$(sudo find / -maxdepth 6 -name "x-ui.db" 2>/dev/null | head -n 1 || true)
if [ -z "$DB" ]; then
  for p in /etc/x-ui/x-ui.db /usr/local/x-ui/x-ui.db /opt/x-ui/x-ui.db; do
    if [ -f "$p" ]; then DB="$p"; break; fi
  done
fi
if [ -z "$DB" ]; then
  echo "NOT_FOUND"
else
  echo "$DB"
fi
"""

def inbound_id_by_port_cmd(db_path: str, port: int) -> str:
    return f"""sudo sqlite3 "{db_path}" "SELECT id FROM inbounds WHERE port={port} ORDER BY id DESC LIMIT 1;" """

async def find_db_path(ssh: Dict[str, Any]) -> Optional[str]:
    code, out, err = await ssh_run_cmd(ssh, FIND_DB_CMD)
    db_path = out.strip().splitlines()[-1] if out.strip() else ""
    if "NOT_FOUND" in db_path or not db_path:
        return None
    return db_path

async def restart_xui(ssh: Dict[str, Any]) -> None:
    await ssh_run_cmd(ssh, "sudo x-ui restart || sudo systemctl restart x-ui || true")

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
  if [ -z "$COLS" ]; then
    echo "ERR_NO_CLIENTS_TABLE"
    exit 11
  fi

  HAS_UUID=$(sudo sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('clients') WHERE name='uuid';")
  if [ "$HAS_UUID" = "0" ]; then
    echo "ERR_NO_UUID"
    exit 12
  fi

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
    for k in ("uuid","id","email","password"):
        v = c.get(k)
        if isinstance(v,str) and v.strip():
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

# ---------------- Backup caption ----------------
def build_backup_caption(server_addr: str, now_utc: datetime) -> str:
    g_date = now_utc.strftime("%Y-%m-%d")
    g_time = now_utc.strftime("%H:%M UTC")
    tehran = now_utc + timedelta(hours=3, minutes=30)
    jy, jm, jd = gregorian_to_jalali(tehran.year, tehran.month, tehran.day)
    j_date = f"{jy:04d}/{jm:02d}/{jd:02d}"
    j_time = tehran.strftime("%H:%M")

    return (
        f"🗂 بکاپ سرور: {server_addr}\n\n"
        f"📅 تاریخ (میلادی): {g_date}\n"
        f"⏰ ساعت: {g_time}\n\n"
        f"📆 تاریخ (شمسی): {to_fa_digits(j_date)}\n"
        f"⏱ ساعت: {to_fa_digits(j_time)}\n\n"
        f"📦 نوع بکاپ: Full x-ui Database\n\n"
        f"🤖 xui_HUB\n"
        f"👨‍💻 Developer: @EmadHabibnia"
    )

# ---------------- UI Texts ----------------
START_TEXT = (
    "🤖 **به xui_HUB خوش آمدید**\n\n"
    "xui_HUB یک ربات حرفه‌ای برای **مدیریت سرورها** و کنترل پنل‌های **3x-ui / x-ui** است.\n\n"
    "از داخل تلگرام می‌توانید:\n"
    "• سرورها را اضافه/ویرایش/حذف کنید\n"
    "• پورت‌ها و کانفیگ‌ها را مدیریت کنید (ادغام کلاینت‌ها)\n"
    "• بکاپ بگیرید یا بکاپ را وارد کنید\n\n"
    "برای شروع از منوی زیر استفاده کنید 👇\n\n"
    "👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)

def one_line_hint(text: str) -> str:
    return f"ℹ️ {text}"

# ---------------- Keyboards ----------------
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠 مدیریت سرورها", callback_data="manage_servers")],
        [InlineKeyboardButton("🔀 مدیریت پورت و کانفیگ", callback_data="merge_menu")],
        [InlineKeyboardButton("🗂 مدیریت بکاپ", callback_data="backup_menu")],
        [InlineKeyboardButton("⬅️ بازگشت به شروع", callback_data="back_main")],
    ])

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به منو", callback_data="back_main")]])

def kb_yes_no(yes_cd: str, no_cd: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله", callback_data=yes_cd),
         InlineKeyboardButton("❌ خیر", callback_data=no_cd)]
    ])

def kb_http_https() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 HTTPS", callback_data="scheme:https"),
         InlineKeyboardButton("🌐 HTTP", callback_data="scheme:http")]
    ])

def display_server_name(s: Dict[str, Any]) -> str:
    panel = s.get("panel") or {}
    host = (panel.get("panel_host") or "").strip()
    if host:
        return host
    return s.get("ssh_host", "server")

def kb_servers_list(store: Dict[str, Any], user_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(store, user_id)
    rows = [[InlineKeyboardButton("➕ اضافه کردن سرور جدید", callback_data="add_server")]]
    for sid in bucket.get("order", []):
        srv = bucket["servers"].get(sid, {})
        label = display_server_name(srv)
        rows.append([
            InlineKeyboardButton(f"🖥 {label}", callback_data=f"server:{sid}"),
            InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_server:{sid}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"del_server:{sid}"),
        ])
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

# ---------------- States (Enum) ----------------
class S(IntEnum):
    # add server
    ADD_SRV_HOST = auto()
    ADD_SRV_SSH_USER = auto()
    ADD_SRV_SSH_PASS = auto()
    ADD_SRV_SSH_PORT = auto()
    ADD_SRV_HAS_PANEL = auto()
    ADD_SRV_PANEL_HOST = auto()
    ADD_SRV_PANEL_PORT = auto()
    ADD_SRV_PANEL_PATH = auto()
    ADD_SRV_PANEL_SCHEME = auto()

    # edit server
    EDIT_SERVER_FIELD = auto()

    # merge
    MERGE_COUNT = auto()
    MERGE_PORTS = auto()
    MERGE_TARGET = auto()
    MERGE_CONFIRM = auto()

    # backup
    BK_EXPORT_PICK = auto()
    BK_IMPORT_MODE = auto()
    BK_IMPORT_PICK = auto()
    BK_IMPORT_UPLOAD = auto()
    BK_IMPORT_CONFIRM = auto()

    BK_NEW_SSH_HOST = auto()
    BK_NEW_SSH_USER = auto()
    BK_NEW_SSH_PASS = auto()
    BK_NEW_SSH_PORT = auto()
    BK_NEW_UPLOAD = auto()

# ---------------- Helpers ----------------
def ssh_from_server(server: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ssh_host": server["ssh_host"],
        "ssh_user": server["ssh_user"],
        "ssh_pass": server["ssh_pass"],
        "ssh_port": int(server["ssh_port"]),
    }

def server_has_panel(server: Dict[str, Any]) -> bool:
    p = server.get("panel") or {}
    return bool((p.get("panel_host") or "").strip()) and bool(p.get("panel_port"))

def panel_addr(server: Dict[str, Any]) -> str:
    p = server.get("panel") or {}
    scheme = p.get("panel_scheme", "https")
    host = p.get("panel_host") or server.get("ssh_host")
    port = p.get("panel_port", "")
    path = p.get("panel_path", "/")
    if path and not str(path).startswith("/"):
        path = "/" + str(path)
    return f"{scheme}://{host}:{port}{path}"

# ---------------- /start ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")

# ---------------- Navigation ----------------
async def nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)

    if q.data == "back_main":
        await q.edit_message_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")
        return

    if q.data == "manage_servers":
        await q.edit_message_text(
            "🛠 **مدیریت سرورها**\n\n"
            "در این بخش می‌توانید سرورهای خود را اضافه کنید و در صورت نیاز اطلاعات پنل x-ui را هم ثبت کنید.\n"
            f"{one_line_hint('فقط اطلاعات سرورها ذخیره می‌شود.')}",
            reply_markup=kb_servers_list(store, uid),
            parse_mode="Markdown"
        )
        return

    if q.data == "merge_menu":
        if not bucket["order"]:
            await q.edit_message_text("اول یک سرور اضافه کنید.", reply_markup=kb_servers_list(store, uid))
            return
        rows = []
        for sid in bucket["order"]:
            srv = bucket["servers"].get(sid, {})
            rows.append([InlineKeyboardButton(f"🔀 {display_server_name(srv)}", callback_data=f"merge_server:{sid}")])
        rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
        await q.edit_message_text(
            "🔀 **مدیریت پورت و کانفیگ**\n\n"
            "سروری که می‌خواهید عملیات ادغام روی آن انجام شود را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown"
        )
        return

    if q.data == "backup_menu":
        await q.edit_message_text(
            "🗂 **مدیریت بکاپ**\n\n"
            "• 📤 گرفتن بکاپ: بکاپ دیتابیس x-ui همین لحظه دریافت می‌شود.\n"
            "• 📥 وارد کردن بکاپ: بازیابی دیتابیس از فایل بکاپ.\n\n"
            f"{one_line_hint('این عملیات از طریق SSH انجام می‌شود.')}",
            reply_markup=kb_backup_menu(),
            parse_mode="Markdown"
        )
        return

    if q.data.startswith("del_server:"):
        sid = q.data.split(":", 1)[1]
        if sid in bucket["servers"]:
            del bucket["servers"][sid]
            bucket["order"] = [x for x in bucket["order"] if x != sid]
            save_store(store)
        await q.edit_message_text("✅ سرور حذف شد.", reply_markup=kb_servers_list(store, uid))
        return

    if q.data.startswith("edit_server:"):
        sid = q.data.split(":", 1)[1]
        if sid not in bucket["servers"]:
            await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_servers_list(store, uid))
            return
        context.user_data.clear()
        context.user_data["edit_sid"] = sid
        await q.edit_message_text(
            "✏️ **ویرایش سرور**\n\n"
            "به شکل زیر ارسال کنید:\n"
            "`field=value`\n\n"
            "فیلدهای SSH:\n"
            "ssh_host, ssh_user, ssh_pass, ssh_port\n\n"
            "فیلدهای پنل (اختیاری):\n"
            "panel_host, panel_port, panel_path, panel_scheme(http/https)\n\n"
            f"{one_line_hint('مثال: ssh_port=22')}",
            parse_mode="Markdown",
            reply_markup=kb_back_main(),
        )
        return S.EDIT_SERVER_FIELD

# ---------------- Add Server Flow ----------------
async def add_server_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data["new_server"] = {"panel": {}}
    await q.edit_message_text(
        "➕ **افزودن سرور جدید**\n\n"
        "🌐 لطفاً **IP یا دامنه سرور** را ارسال کنید.\n"
        f"{one_line_hint('این آدرس برای اتصال SSH استفاده می‌شود.')}",
        parse_mode="Markdown"
    )
    return S.ADD_SRV_HOST

async def add_srv_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ لطفاً IP یا دامنه را ارسال کنید (نه دستور).")
        return S.ADD_SRV_HOST

    context.user_data["new_server"]["ssh_host"] = text
    await update.message.reply_text(
        "👤 **نام کاربری SSH** را ارسال کنید.\n"
        f"{one_line_hint('پیش‌فرض: root — اگر همین است، /skip بزنید.')}",
        parse_mode="Markdown"
    )
    return S.ADD_SRV_SSH_USER

async def add_srv_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ نام کاربری را ارسال کنید یا /skip بزنید.")
        return S.ADD_SRV_SSH_USER

    user = "root" if is_skip(text) else text
    context.user_data["new_server"]["ssh_user"] = user

    await update.message.reply_text(
        "🔑 **رمز عبور SSH** را ارسال کنید.\n"
        f"{one_line_hint('این اطلاعات فقط برای اتصال استفاده می‌شود.')}",
        parse_mode="Markdown"
    )
    return S.ADD_SRV_SSH_PASS

async def add_srv_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ رمز عبور را ارسال کنید (نه دستور).")
        return S.ADD_SRV_SSH_PASS

    context.user_data["new_server"]["ssh_pass"] = text

    await update.message.reply_text(
        "🔢 **پورت SSH** را ارسال کنید.\n"
        f"{one_line_hint('پیش‌فرض: 22 — اگر همین است، /skip بزنید.')}",
        parse_mode="Markdown"
    )
    return S.ADD_SRV_SSH_PORT

async def add_srv_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ پورت را ارسال کنید یا /skip بزنید.")
        return S.ADD_SRV_SSH_PORT

    if is_skip(text):
        port = 22
    else:
        try:
            port = int(text)
            if not (1 <= port <= 65535):
                raise ValueError()
        except:
            await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
            return S.ADD_SRV_SSH_PORT

    context.user_data["new_server"]["ssh_port"] = port

    await update.message.reply_text(
        "✅ اتصال SSH این سرور ثبت شد.\n\n"
        "❓ آیا می‌خواهید **اطلاعات پنل x-ui / 3x-ui** همین سرور را هم اضافه کنید؟",
        reply_markup=kb_yes_no("srv_has_panel_yes", "srv_has_panel_no"),
        parse_mode="Markdown"
    )
    return S.ADD_SRV_HAS_PANEL

async def add_srv_has_panel_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ssh_host = context.user_data["new_server"]["ssh_host"]

    await q.edit_message_text(
        "🌐 **دامنه یا IP پنل** را ارسال کنید.\n"
        f"{one_line_hint('اگر دامنه ندارید، /skip بزنید تا همان IP سرور قرار بگیرد.')}",
        parse_mode="Markdown"
    )
    context.user_data["new_server"]["panel"]["panel_host_default"] = ssh_host
    return S.ADD_SRV_PANEL_HOST

async def add_srv_has_panel_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await finalize_new_server(q, context, include_panel=False)
    return ConversationHandler.END

async def add_srv_panel_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ دامنه/IP پنل را ارسال کنید یا /skip بزنید.")
        return S.ADD_SRV_PANEL_HOST

    default = context.user_data["new_server"]["panel"].get("panel_host_default") or context.user_data["new_server"]["ssh_host"]
    host = default if is_skip(text) else text
    context.user_data["new_server"]["panel"]["panel_host"] = host

    await update.message.reply_text(
        "🔢 **پورت پنل** را ارسال کنید.\n"
        f"{one_line_hint('مثال: 2053 یا 54321')}",
        parse_mode="Markdown"
    )
    return S.ADD_SRV_PANEL_PORT

async def add_srv_panel_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ پورت پنل را ارسال کنید.")
        return S.ADD_SRV_PANEL_PORT

    try:
        port = int(text)
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
        return S.ADD_SRV_PANEL_PORT

    context.user_data["new_server"]["panel"]["panel_port"] = port

    await update.message.reply_text(
        "🧭 **URI Path پنل** را ارسال کنید.\n"
        f"{one_line_hint('اگر پنل path ندارد، /skip بزنید تا / قرار بگیرد.')}",
        parse_mode="Markdown"
    )
    return S.ADD_SRV_PANEL_PATH

async def add_srv_panel_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ مسیر پنل را ارسال کنید یا /skip بزنید.")
        return S.ADD_SRV_PANEL_PATH

    path = "/" if is_skip(text) else text
    if not path.startswith("/"):
        path = "/" + path
    context.user_data["new_server"]["panel"]["panel_path"] = path

    await update.message.reply_text(
        "🔒 **نوع اتصال پنل** را انتخاب کنید:",
        reply_markup=kb_http_https(),
        parse_mode="Markdown"
    )
    return S.ADD_SRV_PANEL_SCHEME

async def add_srv_panel_scheme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    scheme = q.data.split(":", 1)[1].strip()
    if scheme not in ("http", "https"):
        await q.edit_message_text("گزینه نامعتبر. دوباره انتخاب کنید.", reply_markup=kb_http_https())
        return S.ADD_SRV_PANEL_SCHEME

    context.user_data["new_server"]["panel"]["panel_scheme"] = scheme
    await finalize_new_server(q, context, include_panel=True)
    return ConversationHandler.END

async def finalize_new_server(q, context: ContextTypes.DEFAULT_TYPE, include_panel: bool):
    store = load_store()
    user_id = q.from_user.id
    bucket = get_user_bucket(store, user_id)

    srv = context.user_data.get("new_server") or {}
    if not include_panel:
        srv["panel"] = {}

    base = safe_id(srv.get("ssh_host", "server"))
    sid = base
    i = 2
    while sid in bucket["servers"]:
        sid = f"{base}_{i}"
        i += 1

    bucket["servers"][sid] = srv
    bucket["order"].append(sid)
    save_store(store)
    context.user_data.clear()

    label = display_server_name(srv)
    msg = (
        "✅ **سرور با موفقیت اضافه شد**\n\n"
        f"🖥 نام نمایشی: `{label}`\n"
        f"🔗 SSH: `{srv.get('ssh_host')}:{srv.get('ssh_port')}`\n"
    )
    if include_panel and server_has_panel(srv):
        msg += f"\n🌐 پنل: `{panel_addr(srv)}`\n"
    msg += "\nبرای ادامه از منوی اصلی استفاده کنید 👇"

    await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb_main())

# ---------------- Edit Server Flow ----------------
async def edit_server_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = context.user_data.get("edit_sid")
    if not sid:
        await update.message.reply_text("جلسه ویرایش پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ لطفاً `field=value` ارسال کنید (نه دستور).", parse_mode="Markdown")
        return S.EDIT_SERVER_FIELD

    if "=" not in text:
        await update.message.reply_text("فرمت صحیح: `field=value`", parse_mode="Markdown")
        return S.EDIT_SERVER_FIELD

    key, val = text.split("=", 1)
    key = key.strip()
    val = val.strip()

    allowed = {
        "ssh_host", "ssh_user", "ssh_pass", "ssh_port",
        "panel_host", "panel_port", "panel_path", "panel_scheme",
    }
    if key not in allowed:
        await update.message.reply_text("❌ نام فیلد معتبر نیست. دوباره ارسال کنید.")
        return S.EDIT_SERVER_FIELD

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        context.user_data.clear()
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    if key in ("ssh_port", "panel_port"):
        try:
            n = int(val)
            if not (1 <= n <= 65535):
                raise ValueError()
            val = n
        except:
            await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
            return S.EDIT_SERVER_FIELD

    if key == "panel_scheme":
        vv = val.lower()
        if vv not in ("http", "https"):
            await update.message.reply_text("❌ فقط http یا https")
            return S.EDIT_SERVER_FIELD
        val = vv

    if key == "panel_path":
        if not val.startswith("/"):
            val = "/" + val

    if key.startswith("panel_"):
        srv.setdefault("panel", {})
        srv["panel"][key] = val
    else:
        srv[key] = val

    save_store(store)
    context.user_data.clear()
    await update.message.reply_text("✅ ویرایش انجام شد.", reply_markup=kb_main())
    return ConversationHandler.END

# ---------------- Merge Flow ----------------
async def merge_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    sid = q.data.split(":", 1)[1]
    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_servers_list(store, uid))
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["merge"] = {"sid": sid, "ports": []}

    await q.edit_message_text(
        "🔀 **ادغام کلاینت‌ها بین پورت‌ها**\n\n"
        "⚠️ نکته:\n"
        "• پورت مقصد را از قبل داخل پنل ساخته باشید.\n"
        "• عملیات از طریق SSH و دیتابیس x-ui انجام می‌شود.\n\n"
        "✅ ابتدا **تعداد پورت‌های ورودی** را ارسال کنید (مثلاً 2):",
        parse_mode="Markdown"
    )
    return S.MERGE_COUNT

async def merge_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ عدد ارسال کنید (نه دستور).")
        return S.MERGE_COUNT
    try:
        n = int(text)
        if not (1 <= n <= 30):
            raise ValueError()
    except:
        await update.message.reply_text("❌ عدد معتبر (1 تا 30) ارسال کنید.")
        return S.MERGE_COUNT

    context.user_data["merge"]["count"] = n
    context.user_data["merge"]["ports"] = []
    await update.message.reply_text("✅ حالا پورت‌ها را یکی‌یکی ارسال کنید (پورت 1):")
    return S.MERGE_PORTS

async def merge_ports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ پورت ارسال کنید (نه دستور).")
        return S.MERGE_PORTS
    try:
        port = int(text)
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("❌ پورت معتبر ارسال کنید.")
        return S.MERGE_PORTS

    m["ports"].append(port)
    idx = len(m["ports"])
    if idx < m["count"]:
        await update.message.reply_text(f"✅ ثبت شد. پورت بعدی (پورت {idx+1}):")
        return S.MERGE_PORTS

    await update.message.reply_text("✅ همه ورودی‌ها ثبت شد. حالا **پورت مقصد** را ارسال کنید (مثلاً 443):")
    return S.MERGE_TARGET

async def merge_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    text = update.message.text.strip()
    if is_real_command(text):
        await update.message.reply_text("❌ پورت مقصد را ارسال کنید.")
        return S.MERGE_TARGET
    try:
        port = int(text)
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("❌ پورت مقصد معتبر ارسال کنید.")
        return S.MERGE_TARGET

    m["target_port"] = port
    await update.message.reply_text(
        "🧾 **خلاصه عملیات**\n\n"
        f"ورودی‌ها: `{m['ports']}`\n"
        f"مقصد: `{m['target_port']}`\n\n"
        "اگر آماده‌اید برای اجرا عبارت زیر را ارسال کنید:\n"
        "`OK`",
        parse_mode="Markdown"
    )
    return S.MERGE_CONFIRM

async def merge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "ok":
        await update.message.reply_text("برای ادامه فقط `OK` ارسال کنید.", parse_mode="Markdown")
        return S.MERGE_CONFIRM

    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)

    sid = context.user_data["merge"]["sid"]
    srv = bucket["servers"].get(sid)
    if not srv:
        context.user_data.clear()
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    ssh = ssh_from_server(srv)
    src_ports = context.user_data["merge"]["ports"]
    target_port = context.user_data["merge"]["target_port"]

    await update.message.reply_text("⏳ در حال اتصال و انجام ادغام...")

    db_path = await find_db_path(ssh)
    if not db_path:
        context.user_data.clear()
        await update.message.reply_text(
            "❌ دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم.\n"
            f"{one_line_hint('مطمئن شوید x-ui نصب است و کاربر SSH دسترسی sudo دارد.')}",
            reply_markup=kb_main()
        )
        return ConversationHandler.END

    def get_inbound_id(port: int) -> Optional[int]:
        c, o, e = ssh_exec(ssh["ssh_host"], ssh["ssh_port"], ssh["ssh_user"], ssh["ssh_pass"], inbound_id_by_port_cmd(db_path, port))
        v = (o or "").strip()
        return int(v) if v.isdigit() else None

    target_id = await asyncio.to_thread(get_inbound_id, target_port)
    if not target_id:
        context.user_data.clear()
        await update.message.reply_text(
            f"❌ inbound مقصد با پورت {target_port} پیدا نشد.\n"
            f"{one_line_hint('اول داخل پنل، inbound مقصد را بسازید.')}",
            reply_markup=kb_main()
        )
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

    code, out, err = await ssh_run_cmd(ssh, remote_cmd)
    if code != 0:
        context.user_data.clear()
        msg = (out + "\n" + err).strip()
        await update.message.reply_text(f"❌ خطا:\n{msg[:3500]}", reply_markup=kb_main())
        return ConversationHandler.END

    await restart_xui(ssh)

    context.user_data.clear()
    await update.message.reply_text(f"✅ ادغام انجام شد.\n\n{out.strip()}", reply_markup=kb_main())
    return ConversationHandler.END

# ---------------- Backup (Export only minimal + Import modes skeleton) ----------------
async def backup_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)

    if q.data == "backup_menu":
        await q.edit_message_text(
            "🗂 **مدیریت بکاپ**\n\n"
            "• 📤 گرفتن بکاپ: بکاپ دیتابیس x-ui همین لحظه دریافت می‌شود.\n"
            "• 📥 وارد کردن بکاپ: بازیابی دیتابیس از فایل بکاپ.\n\n"
            f"{one_line_hint('این عملیات از طریق SSH انجام می‌شود.')}",
            reply_markup=kb_backup_menu(),
            parse_mode="Markdown"
        )
        return

    if q.data == "bk_export":
        if not bucket["order"]:
            await q.edit_message_text("اول یک سرور اضافه کنید.", reply_markup=kb_servers_list(store, uid))
            return

        rows = []
        for sid in bucket["order"]:
            srv = bucket["servers"].get(sid, {})
            rows.append([InlineKeyboardButton(f"📤 {display_server_name(srv)}", callback_data=f"bk_export:{sid}")])
        rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])
        await q.edit_message_text("📤 سرور موردنظر برای بکاپ را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
        return S.BK_EXPORT_PICK

    if q.data.startswith("bk_export:"):
        sid = q.data.split(":", 1)[1]
        srv = bucket["servers"].get(sid)
        if not srv:
            await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
            return ConversationHandler.END

        ssh = ssh_from_server(srv)
        await q.edit_message_text("⏳ در حال گرفتن بکاپ...")

        db_path = await find_db_path(ssh)
        if not db_path:
            await q.edit_message_text(
                "❌ دیتابیس x-ui.db پیدا نشد یا sudo ندارم.\n"
                f"{one_line_hint('برای گرفتن بکاپ باید x-ui نصب باشد و کاربر SSH دسترسی sudo داشته باشد.')}",
                reply_markup=kb_main()
            )
            return ConversationHandler.END

        now_utc = datetime.now(timezone.utc)
        ts = now_utc.strftime("%Y%m%d_%H%M")
        remote_tmp = f"/tmp/xuihub_backup_{ts}.db"

        remote_cmd = f"""
set -e
sudo cp "{db_path}" "{remote_tmp}"
sudo chmod 644 "{remote_tmp}" || true
echo "{remote_tmp}"
"""
        code, out, err = await ssh_run_cmd(ssh, remote_cmd)
        if code != 0:
            msg = (out + "\n" + err).strip()
            await q.edit_message_text(f"❌ خطا:\n{msg[:3500]}", reply_markup=kb_main())
            return ConversationHandler.END

        remote_file = out.strip().splitlines()[-1] if out.strip() else remote_tmp

        local_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="xuihub_backup_", suffix=".db", delete=False) as f:
                local_path = f.name

            def sftp_download():
                c = ssh_client(ssh["ssh_host"], ssh["ssh_port"], ssh["ssh_user"], ssh["ssh_pass"])
                sftp = c.open_sftp()
                sftp.get(remote_file, local_path)
                sftp.close()
                c.close()

            await asyncio.to_thread(sftp_download)
        except Exception as e:
            await q.edit_message_text(f"❌ خطا در دانلود بکاپ: {e}", reply_markup=kb_main())
            return ConversationHandler.END
        finally:
            await ssh_run_cmd(ssh, f"sudo rm -f '{remote_file}' || true")

        caption = build_backup_caption(display_server_name(srv), now_utc)
        filename = f"xui_backup_{display_server_name(srv)}_{ts}.db".replace("/", "_").replace(":", "_")

        try:
            await q.edit_message_text("✅ بکاپ آماده شد. در حال ارسال...")
            await q.message.reply_document(
                document=InputFile(local_path, filename=filename),
                caption=caption
            )
            await q.message.reply_text("✅ انجام شد.", reply_markup=kb_main())
        finally:
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except:
                pass
        return ConversationHandler.END

    if q.data == "bk_import":
        await q.edit_message_text(
            "📥 **وارد کردن بکاپ (Restore)**\n\n"
            "🔸 این بخش در نسخه بعدی کامل می‌شود.\n"
            f"{one_line_hint('الان فقط گرفتن بکاپ فعال است.')}",
            reply_markup=kb_backup_menu(),
            parse_mode="Markdown"
        )
        return

# ---------------- Main ----------------
def main():
    token = env_required("TOKEN")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))

    # Add Server Conversation
    conv_add_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_server_entry, pattern="^add_server$")],
        states={
            S.ADD_SRV_HOST: [MessageHandler(filters.TEXT, add_srv_host)],
            S.ADD_SRV_SSH_USER: [MessageHandler(filters.TEXT, add_srv_ssh_user)],
            S.ADD_SRV_SSH_PASS: [MessageHandler(filters.TEXT, add_srv_ssh_pass)],
            S.ADD_SRV_SSH_PORT: [MessageHandler(filters.TEXT, add_srv_ssh_port)],
            S.ADD_SRV_HAS_PANEL: [
                CallbackQueryHandler(add_srv_has_panel_yes, pattern="^srv_has_panel_yes$"),
                CallbackQueryHandler(add_srv_has_panel_no, pattern="^srv_has_panel_no$"),
            ],
            S.ADD_SRV_PANEL_HOST: [MessageHandler(filters.TEXT, add_srv_panel_host)],
            S.ADD_SRV_PANEL_PORT: [MessageHandler(filters.TEXT, add_srv_panel_port)],
            S.ADD_SRV_PANEL_PATH: [MessageHandler(filters.TEXT, add_srv_panel_path)],
            S.ADD_SRV_PANEL_SCHEME: [CallbackQueryHandler(add_srv_panel_scheme, pattern=r"^scheme:(http|https)$")],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_add_server)

    # Edit Server Conversation (entry via button)
    conv_edit_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(nav, pattern=r"^edit_server:")],
        states={
            S.EDIT_SERVER_FIELD: [MessageHandler(filters.TEXT, edit_server_field)]
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_edit_server)

    # Merge Conversation
    conv_merge = ConversationHandler(
        entry_points=[CallbackQueryHandler(merge_entry, pattern=r"^merge_server:")],
        states={
            S.MERGE_COUNT: [MessageHandler(filters.TEXT, merge_count)],
            S.MERGE_PORTS: [MessageHandler(filters.TEXT, merge_ports)],
            S.MERGE_TARGET: [MessageHandler(filters.TEXT, merge_target)],
            S.MERGE_CONFIRM: [MessageHandler(filters.TEXT, merge_confirm)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_merge)

    # Backup handlers
    app.add_handler(CallbackQueryHandler(backup_router, pattern=r"^(backup_menu|bk_export|bk_import|bk_export:.*)$"))

    # Main navigation (after conversations)
    app.add_handler(CallbackQueryHandler(nav))

    app.run_polling()

if __name__ == "__main__":
    main()
