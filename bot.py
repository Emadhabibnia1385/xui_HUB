import os
import json
import re
import asyncio
import tempfile
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple

import paramiko
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("xuihub")


STORE_FILE = "store.json"


# ------------------------- Storage (only panels are stored) -------------------------
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
    store["users"].setdefault(uid, {"panels": {}, "order": []})
    return store["users"][uid]


def safe_panel_id(host: str) -> str:
    pid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", host.strip())
    return pid or "panel"


# ------------------------- Jalali (Shamsi) conversion -------------------------
def gregorian_to_jalali(gy: int, gm: int, gd: int) -> Tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy = 979
        gy -= 1600
    else:
        jy = 0
        gy -= 621

    gy2 = gy + 1 if gm > 2 else gy
    days = (
        (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        - 80
        + gd
        + g_d_m[gm - 1]
    )

    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461

    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365

    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)

    return jy, jm, jd


PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_fa_digits(s: str) -> str:
    return s.translate(PERSIAN_DIGITS)


# ------------------------- SSH helpers (ROBUST + TIMEOUT) -------------------------
def ssh_client(host: str, port: int, user: str, password: str, timeout: int = 20) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # banner_timeout / auth_timeout برای جلوگیری از هنگ
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
    """
    read_timeout: برای اینکه stdout.read() گیر نکند.
    """
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    # timeout روی کانال‌ها
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
        code, out, err = ssh_exec_raw(c, cmd, read_timeout=read_timeout)
    finally:
        c.close()
    return code, out, err


# ------------------------- FAST FIND DB (NO HANG) -------------------------
# اول مسیرهای رایج، بعد یک find زمان‌دار
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


def inbound_id_by_port_cmd(db_path: str, port: int) -> str:
    return f"""sudo sqlite3 "{db_path}" "SELECT id FROM inbounds WHERE port={port} ORDER BY id DESC LIMIT 1;" """


def make_merge_script() -> str:
    # همون اسکریپت شماست فقط تمیز
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


# ------------------------- Telegram states -------------------------
(
    ADD_IP,
    ADD_HTTP,
    ADD_PANEL_PORT,
    ADD_PATH,
    ADD_USER,
    ADD_PASS,
    ADD_SSH_HOST,
    ADD_SSH_USER,
    ADD_SSH_PORT,
    ADD_SSH_PASS,
    MERGE_COUNT,
    MERGE_PORTS,
    MERGE_TARGET,
    MERGE_CONFIRM,
    BK_MENU,
    BK_EXPORT_PICK_PANEL,
    BK_IMPORT_CHOOSE_MODE,
    BK_IMPORT_PICK_PANEL,
    BK_IMPORT_UPLOAD_FILE,
    BK_IMPORT_CONFIRM,
    BK_IMPORT_NEW_SSH_HOST,
    BK_IMPORT_NEW_SSH_USER,
    BK_IMPORT_NEW_SSH_PORT,
    BK_IMPORT_NEW_SSH_PASS,
    BK_IMPORT_NEW_UPLOAD_FILE,
    BK_IMPORT_NEW_CONFIRM,
    EDIT_VALUE,
) = range(27)


# ------------------------- Keyboards -------------------------
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛠 مدیریت پنل‌ها", callback_data="manage_panels")],
            [InlineKeyboardButton("🔀 مدیریت پورت و کانفیگ", callback_data="start_merge")],
            [InlineKeyboardButton("🗂 مدیریت بکاپ", callback_data="backup_menu")],
            [InlineKeyboardButton("👤 پروفایل", callback_data="profile")],
        ]
    )


def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])


def kb_panels(store: Dict[str, Any], user_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(store, user_id)
    rows = [[InlineKeyboardButton("➕ اضافه کردن پنل", callback_data="add_panel")]]

    for pid in bucket.get("order", []):
        rows.append(
            [
                InlineKeyboardButton(f"📌 {pid}", callback_data=f"panel:{pid}"),
                InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit:{pid}"),
                InlineKeyboardButton("🗑 حذف", callback_data=f"del:{pid}"),
            ]
        )

    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_panel_actions(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔀 ادغام کلاینت/پورت‌ها", callback_data=f"merge:{pid}")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="manage_panels")],
        ]
    )


def kb_backup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 گرفتن بکاپ", callback_data="bk_export")],
            [InlineKeyboardButton("📥 وارد کردن بکاپ", callback_data="bk_import")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")],
        ]
    )


def kb_backup_import_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔁 از سرورهای موجود", callback_data="bk_import_existing")],
            [InlineKeyboardButton("➕ سرور جدید (بدون ذخیره)", callback_data="bk_import_new")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")],
        ]
    )


def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v


# ------------------------- UI Text -------------------------
START_TEXT = (
    "🤖 **به xui_HUB خوش آمدید**\n\n"
    "xui_HUB یک ربات حرفه‌ای برای مدیریت پنل‌های **3x-ui / x-ui** است.\n"
    "از داخل تلگرام می‌توانید **پنل‌ها، پورت‌ها، کانفیگ‌ها و بکاپ‌ها** را مدیریت کنید.\n\n"
    "برای شروع، از منوی زیر استفاده کنید 👇\n\n"
    "👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)


# ------------------------- Helpers for safe user error messages -------------------------
def _short(s: str, n: int = 1800) -> str:
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


async def report_error(update: Update, title: str, detail: str, extra: str = ""):
    msg = f"❌ **{title}**\n\n{detail}"
    if extra.strip():
        msg += f"\n\n```text\n{_short(extra, 1500)}\n```"
    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")
    elif update.callback_query:
        try:
            await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
        except Exception:
            pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")


# ------------------------- Navigation callbacks (only non-conv buttons) -------------------------
async def nav_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    if q.data == "back_main":
        await q.edit_message_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")
        return

    if q.data == "manage_panels":
        await q.edit_message_text(
            "🛠 **مدیریت پنل‌ها**\n\nاز این بخش می‌توانید پنل‌ها را اضافه/ویرایش/حذف کنید.",
            reply_markup=kb_panels(store, user_id),
            parse_mode="Markdown",
        )
        return

    if q.data == "start_merge":
        if not bucket["order"]:
            await q.edit_message_text("ابتدا یک پنل اضافه کنید.", reply_markup=kb_panels(store, user_id))
            return

        rows = [[InlineKeyboardButton(f"🔀 {pid}", callback_data=f"merge:{pid}")] for pid in bucket["order"]]
        rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
        await q.edit_message_text(
            "🔀 **مدیریت پورت و کانفیگ**\n\nپنلی که می‌خواهید عملیات روی آن انجام شود را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        return

    if q.data == "profile":
        u = update.effective_user
        username = f"@{u.username}" if u.username else "ندارد"
        panels_count = len(bucket.get("order", []))
        panel_list = "\n".join([f"• {p}" for p in bucket.get("order", [])]) if panels_count else "—"
        text = (
            "👤 **پروفایل شما**\n\n"
            f"نام: {u.full_name}\n"
            f"یوزرنیم: {username}\n"
            f"User ID: {u.id}\n\n"
            f"تعداد پنل‌ها: {panels_count}\n"
            f"لیست پنل‌ها:\n{panel_list}"
        )
        await q.edit_message_text(text, reply_markup=kb_back_main(), parse_mode="Markdown")
        return

    if q.data.startswith("panel:"):
        pid = q.data.split(":", 1)[1]
        if pid not in bucket["panels"]:
            await q.edit_message_text("پنل پیدا نشد.", reply_markup=kb_panels(store, user_id))
            return

        context.user_data.clear()
        context.user_data["selected_pid"] = pid
        await q.edit_message_text(
            f"📌 پنل انتخاب شد: **{pid}**",
            reply_markup=kb_panel_actions(pid),
            parse_mode="Markdown",
        )
        return

    if q.data.startswith("del:"):
        pid = q.data.split(":", 1)[1]
        if pid in bucket["panels"]:
            del bucket["panels"][pid]
            bucket["order"] = [x for x in bucket["order"] if x != pid]
            save_store(store)
        await q.edit_message_text("✅ پنل حذف شد.", reply_markup=kb_panels(store, user_id))
        return

    if q.data.startswith("edit:"):
        pid = q.data.split(":", 1)[1]
        if pid not in bucket["panels"]:
            await q.edit_message_text("پنل پیدا نشد.", reply_markup=kb_panels(store, user_id))
            return

        context.user_data.clear()
        context.user_data["edit_pid"] = pid
        await q.edit_message_text(
            "✏️ **ویرایش پنل**\n\n"
            "به این شکل ارسال کنید:\n"
            "field=value\n\n"
            "فیلدها:\n"
            "panel_host, panel_scheme(http/https), panel_port, panel_path,\n"
            "panel_user, panel_pass, ssh_host, ssh_user, ssh_port, ssh_pass\n\n"
            "مثال:\n"
            "ssh_port=22",
            parse_mode="Markdown",
        )
        return


# ------------------------- Add panel flow -------------------------
async def add_panel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text(
        "🛠 **افزودن پنل جدید**\n\nلطفاً **آیپی یا دامنه پنل** را ارسال کنید:",
        parse_mode="Markdown",
    )
    return ADD_IP


async def add_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"] = {"panel_host": update.message.text.strip()}
    await update.message.reply_text("🔒 نوع دسترسی پنل: HTTP یا HTTPS", parse_mode="Markdown")
    return ADD_HTTP


async def add_http(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = update.message.text.strip().lower()
    if v not in ("http", "https"):
        await update.message.reply_text("فقط HTTP یا HTTPS ارسال کنید.", parse_mode="Markdown")
        return ADD_HTTP
    context.user_data["new_panel"]["panel_scheme"] = v
    await update.message.reply_text("🔢 پورت پنل را ارسال کنید:")
    return ADD_PANEL_PORT


async def add_panel_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت معتبر ارسال کنید (1..65535).")
        return ADD_PANEL_PORT
    context.user_data["new_panel"]["panel_port"] = port
    await update.message.reply_text("🧭 پچ پنل (مثلاً /panel) — اگر ندارید /:")
    return ADD_PATH


async def add_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = update.message.text.strip()
    if not path.startswith("/"):
        path = "/" + path
    context.user_data["new_panel"]["panel_path"] = path
    await update.message.reply_text("👤 یوزرنیم پنل:")
    return ADD_USER


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["panel_user"] = update.message.text.strip()
    await update.message.reply_text("🔑 پسورد پنل:")
    return ADD_PASS


async def add_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["panel_pass"] = update.message.text.strip()
    await update.message.reply_text("🌐 SSH Host (آیپی/دامنه سرور):")
    return ADD_SSH_HOST


async def add_ssh_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["ssh_host"] = update.message.text.strip()
    await update.message.reply_text("👤 SSH Username:")
    return ADD_SSH_USER


async def add_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["ssh_user"] = update.message.text.strip()
    await update.message.reply_text("🔢 SSH Port (مثلاً 22):")
    return ADD_SSH_PORT


async def add_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت معتبر ارسال کنید (1..65535).")
        return ADD_SSH_PORT
    context.user_data["new_panel"]["ssh_port"] = port
    await update.message.reply_text("🔑 SSH Password:")
    return ADD_SSH_PASS


async def add_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["ssh_pass"] = update.message.text.strip()
    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    host = context.user_data["new_panel"]["panel_host"]
    pid = safe_panel_id(host)
    base = pid
    i = 2
    while pid in bucket["panels"]:
        pid = f"{base}_{i}"
        i += 1

    bucket["panels"][pid] = context.user_data["new_panel"]
    bucket["order"].append(pid)
    save_store(store)

    context.user_data.clear()
    await update.message.reply_text("✅ پنل ذخیره شد.", reply_markup=kb_main())
    return ConversationHandler.END


# ------------------------- Edit flow (field=value) -------------------------
async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = context.user_data.get("edit_pid")
    if not pid:
        return ConversationHandler.END

    text = update.message.text.strip()
    if "=" not in text:
        await update.message.reply_text("فرمت صحیح: field=value", parse_mode="Markdown")
        return EDIT_VALUE

    key, val = text.split("=", 1)
    key = key.strip()
    val = val.strip()

    allowed = {
        "panel_host",
        "panel_scheme",
        "panel_port",
        "panel_path",
        "panel_user",
        "panel_pass",
        "ssh_host",
        "ssh_user",
        "ssh_port",
        "ssh_pass",
    }
    if key not in allowed:
        await update.message.reply_text("نام فیلد اشتباه است. دوباره ارسال کنید.")
        return EDIT_VALUE

    if key in ("panel_port", "ssh_port"):
        try:
            v = int(val)
            if not (1 <= v <= 65535):
                raise ValueError()
            val = v
        except Exception:
            await update.message.reply_text("پورت معتبر ارسال کنید (1..65535).")
            return EDIT_VALUE

    if key == "panel_scheme":
        v = val.lower()
        if v not in ("http", "https"):
            await update.message.reply_text("فقط http یا https", parse_mode="Markdown")
            return EDIT_VALUE
        val = v

    if key == "panel_path":
        if not val.startswith("/"):
            val = "/" + val

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    if pid not in bucket["panels"]:
        context.user_data.clear()
        await update.message.reply_text("پنل پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    bucket["panels"][pid][key] = val
    save_store(store)

    context.user_data.clear()
    await update.message.reply_text("✅ ویرایش انجام شد.", reply_markup=kb_main())
    return ConversationHandler.END


# ------------------------- Merge flow (ROBUST) -------------------------
async def merge_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    pid = q.data.split(":", 1)[1]
    if pid not in bucket["panels"]:
        await q.edit_message_text("پنل پیدا نشد.", reply_markup=kb_panels(store, user_id))
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["merge"] = {"panel_id": pid, "ports": []}

    await q.edit_message_text(
        "🔀 **ادغام پورت‌ها**\n\n"
        "⚠️ پورت مقصد را **از قبل** داخل پنل ساخته باشید.\n\n"
        "تعداد پورت‌های ورودی را ارسال کنید (مثلاً 2):",
        parse_mode="Markdown",
    )
    return MERGE_COUNT


async def merge_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if not (1 <= n <= 30):
            raise ValueError()
    except Exception:
        await update.message.reply_text("عدد معتبر (1 تا 30) ارسال کنید.")
        return MERGE_COUNT

    context.user_data["merge"]["count"] = n
    context.user_data["merge"]["ports"] = []
    await update.message.reply_text("✅ حالا پورت‌ها را یکی‌یکی ارسال کنید (پورت 1):")
    return MERGE_PORTS


async def merge_ports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت معتبر ارسال کنید.")
        return MERGE_PORTS

    m["ports"].append(port)
    idx = len(m["ports"])
    if idx < m["count"]:
        await update.message.reply_text(f"✅ پورت {idx} ثبت شد. پورت بعدی (پورت {idx + 1}):")
        return MERGE_PORTS

    await update.message.reply_text("✅ همه ورودی‌ها ثبت شد. پورت مقصد را ارسال کنید (مثلاً 443):")
    return MERGE_TARGET


async def merge_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت مقصد معتبر ارسال کنید.")
        return MERGE_TARGET

    m["target_port"] = port
    await update.message.reply_text(
        "🧾 **خلاصه عملیات**\n\n"
        f"ورودی‌ها: {m['ports']}\n"
        f"مقصد: {m['target_port']}\n\n"
        "برای اجرا OK ارسال کنید:",
        parse_mode="Markdown",
    )
    return MERGE_CONFIRM


async def merge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "ok":
        await update.message.reply_text("برای ادامه فقط OK ارسال کنید.", parse_mode="Markdown")
        return MERGE_CONFIRM

    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    pid = context.user_data.get("merge", {}).get("panel_id")
    panel = bucket["panels"].get(pid) if pid else None
    if not panel:
        context.user_data.clear()
        await update.message.reply_text("❌ پنل پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    src_ports = context.user_data["merge"]["ports"]
    target_port = context.user_data["merge"]["target_port"]

    # پیام‌های مرحله‌ای
    await update.message.reply_text("⏳ شروع ادغام... (اتصال SSH)")
    try:
        # 1) Find DB with timeout wrapper
        await update.message.reply_text("🔎 در حال پیدا کردن دیتابیس x-ui.db ...")
        try:
            code, out, err = await asyncio.wait_for(
                asyncio.to_thread(
                    ssh_exec,
                    panel["ssh_host"],
                    panel["ssh_port"],
                    panel["ssh_user"],
                    panel["ssh_pass"],
                    FIND_DB_CMD,
                ),
                timeout=45,
            )
        except asyncio.TimeoutError:
            context.user_data.clear()
            await report_error(
                update,
                "Timeout در پیدا کردن دیتابیس",
                "سرور پاسخ نداد یا دستور جستجوی دیتابیس طولانی شد.",
                "راه‌حل: مسیر دیتابیس را در سرور ثابت کنید یا timeout را بالا ببرید.",
            )
            return ConversationHandler.END

        db_path = out.strip().splitlines()[-1] if out.strip() else ""
        if code != 0:
            context.user_data.clear()
            await report_error(update, "خطا در اجرای دستور پیدا کردن DB", "کد خروجی غیر صفر.", out + "\n" + err)
            return ConversationHandler.END

        if "NOT_FOUND" in db_path or not db_path:
            context.user_data.clear()
            await report_error(
                update,
                "دیتابیس پیدا نشد",
                "x-ui.db پیدا نشد یا دسترسی sudo ندارید.",
                out + "\n" + err,
            )
            return ConversationHandler.END

        await update.message.reply_text(f"✅ دیتابیس پیدا شد:\n`{db_path}`", parse_mode="Markdown")

        # 2) inbound ids
        async def get_inbound_id(port: int) -> Optional[int]:
            cmd = inbound_id_by_port_cmd(db_path, port)
            try:
                code2, o2, e2 = await asyncio.wait_for(
                    asyncio.to_thread(
                        ssh_exec,
                        panel["ssh_host"],
                        panel["ssh_port"],
                        panel["ssh_user"],
                        panel["ssh_pass"],
                        cmd,
                    ),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(f"Timeout در گرفتن inbound برای پورت {port}")

            v = (o2 or "").strip()
            return int(v) if v.isdigit() else None

        await update.message.reply_text("🔎 در حال پیدا کردن inbound مقصد ...")
        try:
            target_id = await get_inbound_id(target_port)
        except Exception as e:
            context.user_data.clear()
            await report_error(update, "خطا در گرفتن inbound مقصد", str(e))
            return ConversationHandler.END

        if not target_id:
            context.user_data.clear()
            await report_error(
                update,
                "inbound مقصد پیدا نشد",
                f"inbound مقصد با پورت {target_port} پیدا نشد. اول داخل پنل بساز.",
            )
            return ConversationHandler.END

        await update.message.reply_text(f"✅ inbound مقصد: {target_id}")

        await update.message.reply_text("🔎 در حال پیدا کردن inbound ورودی‌ها ...")
        source_ids = []
        missing = []
        for p in src_ports:
            try:
                iid = await get_inbound_id(p)
            except Exception as e:
                context.user_data.clear()
                await report_error(update, "خطا در گرفتن inbound ورودی", f"پورت {p}: {e}")
                return ConversationHandler.END

            if not iid:
                missing.append(p)
            else:
                source_ids.append(iid)

        if missing:
            context.user_data.clear()
            await report_error(update, "برخی پورت‌ها پیدا نشدند", f"این پورت‌ها inbound ندارند: {missing}")
            return ConversationHandler.END

        src_ids_csv = ",".join(str(x) for x in source_ids)

        # 3) run merge script
        await update.message.reply_text("🧩 در حال اجرای اسکریپت ادغام روی سرور ...")
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
            code3, out3, err3 = await asyncio.wait_for(
                asyncio.to_thread(
                    ssh_exec,
                    panel["ssh_host"],
                    panel["ssh_port"],
                    panel["ssh_user"],
                    panel["ssh_pass"],
                    remote_cmd,
                ),
                timeout=70,
            )
        except asyncio.TimeoutError:
            context.user_data.clear()
            await report_error(
                update,
                "Timeout در اجرای Merge",
                "اسکریپت ادغام زمان‌بر شد یا گیر کرد.",
                "احتمالاً sudo پسورد می‌خواهد یا sqlite3/DB مشکل دارد.",
            )
            return ConversationHandler.END

        if code3 != 0:
            context.user_data.clear()
            await report_error(update, "ادغام با خطا انجام شد", "کد خروجی غیر صفر.", out3 + "\n" + err3)
            return ConversationHandler.END

        await update.message.reply_text(f"✅ خروجی ادغام:\n```text\n{_short(out3, 1600)}\n```", parse_mode="Markdown")

        # 4) restart x-ui
        await update.message.reply_text("🔄 در حال ریستارت سرویس x-ui ...")
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    ssh_exec,
                    panel["ssh_host"],
                    panel["ssh_port"],
                    panel["ssh_user"],
                    panel["ssh_pass"],
                    "sudo x-ui restart || sudo systemctl restart x-ui || true",
                ),
                timeout=35,
            )
        except asyncio.TimeoutError:
            # ریستارت اگر تایم اوت شد هم ادغام انجام شده؛ فقط اطلاع بده
            await update.message.reply_text("⚠️ ریستارت سرویس طولانی شد/timeout خورد، ولی ادغام انجام شده است.")

        context.user_data.clear()
        await update.message.reply_text("🎉 **ادغام با موفقیت انجام شد.**", parse_mode="Markdown", reply_markup=kb_main())
        return ConversationHandler.END

    except Exception as e:
        logger.exception("merge_confirm crashed")
        context.user_data.clear()
        await report_error(update, "خطای غیرمنتظره در Merge", str(e))
        await update.message.reply_text("برای ادامه از منوی اصلی استفاده کنید 👇", reply_markup=kb_main())
        return ConversationHandler.END


# ------------------------- Backup helpers -------------------------
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
        f"🤖 xui_HUB\n"
        f"👨‍💻 Developer: @EmadHabibnia"
    )


async def find_db_path(panel: Dict[str, Any]) -> Optional[str]:
    code, out, err = await asyncio.to_thread(
        ssh_exec,
        panel["ssh_host"],
        panel["ssh_port"],
        panel["ssh_user"],
        panel["ssh_pass"],
        FIND_DB_CMD,
    )
    db_path = out.strip().splitlines()[-1] if out.strip() else ""
    if code != 0:
        return None
    if "NOT_FOUND" in db_path or not db_path:
        return None
    return db_path


async def restart_xui(panel: Dict[str, Any]) -> None:
    await asyncio.to_thread(
        ssh_exec,
        panel["ssh_host"],
        panel["ssh_port"],
        panel["ssh_user"],
        panel["ssh_pass"],
        "sudo x-ui restart || sudo systemctl restart x-ui || true",
    )


# ------------------------- Backup flow (same as before, but safe) -------------------------
async def backup_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🗂 **مدیریت بکاپ**\n\n"
        "• 📤 گرفتن بکاپ: بکاپ کامل پنل را همین لحظه دریافت می‌کنید.\n"
        "• 📥 وارد کردن بکاپ: بازیابی دیتابیس از فایل بکاپ.\n\n"
        "⚠️ این عملیات از طریق SSH انجام می‌شود.",
        reply_markup=kb_backup_menu(),
        parse_mode="Markdown",
    )
    return BK_MENU


async def bk_export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    if not bucket["order"]:
        await q.edit_message_text("ابتدا یک پنل اضافه کنید.", reply_markup=kb_panels(store, user_id))
        return ConversationHandler.END

    rows = [[InlineKeyboardButton(f"📤 {pid}", callback_data=f"bk_export_panel:{pid}")] for pid in bucket["order"]]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])
    await q.edit_message_text("📤 پنل موردنظر برای بکاپ را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
    return BK_EXPORT_PICK_PANEL


async def bk_export_pick_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = q.data.split(":", 1)[1]

    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)
    panel = bucket["panels"].get(pid)

    if not panel:
        await q.edit_message_text("پنل پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    await q.edit_message_text("⏳ در حال گرفتن بکاپ...")
    db_path = await find_db_path(panel)
    if not db_path:
        await q.edit_message_text("❌ دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم.", reply_markup=kb_main())
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

    code, out, err = await asyncio.to_thread(
        ssh_exec,
        panel["ssh_host"],
        panel["ssh_port"],
        panel["ssh_user"],
        panel["ssh_pass"],
        remote_cmd,
    )
    if code != 0:
        msg = (out + "\n" + err).strip()
        await q.edit_message_text(f"❌ خطا:\n{_short(msg, 3500)}", reply_markup=kb_main())
        return ConversationHandler.END

    remote_file = out.strip().splitlines()[-1] if out.strip() else remote_tmp
    local_path = None

    try:
        with tempfile.NamedTemporaryFile(prefix="xuihub_backup_", suffix=".db", delete=False) as f:
            local_path = f.name

        def sftp_download():
            c = ssh_client(panel["ssh_host"], panel["ssh_port"], panel["ssh_user"], panel["ssh_pass"])
            sftp = c.open_sftp()
            sftp.get(remote_file, local_path)
            sftp.close()
            c.close()

        await asyncio.to_thread(sftp_download)

    except Exception as e:
        await q.edit_message_text(f"❌ خطا در دانلود بکاپ: {e}", reply_markup=kb_main())
        return ConversationHandler.END

    finally:
        await asyncio.to_thread(
            ssh_exec,
            panel["ssh_host"],
            panel["ssh_port"],
            panel["ssh_user"],
            panel["ssh_pass"],
            f"sudo rm -f '{remote_file}' || true",
        )

    caption = build_backup_caption(panel.get("panel_host", pid), now_utc)
    filename = f"xui_backup_{panel.get('panel_host', pid)}_{ts}.db".replace("/", "_").replace(":", "_")

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
    await q.edit_message_text(
        "📥 **وارد کردن بکاپ (Restore)**\n\nروش بازیابی را انتخاب کنید:",
        reply_markup=kb_backup_import_mode(),
        parse_mode="Markdown",
    )
    return BK_IMPORT_CHOOSE_MODE


async def bk_import_existing_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    if not bucket["order"]:
        await q.edit_message_text("ابتدا یک پنل اضافه کنید.", reply_markup=kb_panels(store, user_id))
        return ConversationHandler.END

    context.user_data.clear()
    rows = [[InlineKeyboardButton(f"🔁 {pid}", callback_data=f"bk_import_panel:{pid}")] for pid in bucket["order"]]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])
    await q.edit_message_text("🔁 پنل مقصد برای Restore را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
    return BK_IMPORT_PICK_PANEL


async def bk_import_pick_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = q.data.split(":", 1)[1]

    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)
    panel = bucket["panels"].get(pid)

    if not panel:
        await q.edit_message_text("پنل پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    context.user_data["bk_target_panel"] = panel
    await q.edit_message_text(
        "📎 لطفاً **فایل بکاپ دیتابیس** را ارسال کنید (فایل .db).\n\n"
        "⚠️ این عملیات دیتابیس فعلی را جایگزین می‌کند.",
        parse_mode="Markdown",
    )
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
        "⚠️ **هشدار مهم**\n\n"
        "این عملیات دیتابیس فعلی پنل را به‌طور کامل جایگزین می‌کند.\n"
        "اگر مطمئن هستید، عبارت زیر را ارسال کنید:\n"
        "RESTORE",
        parse_mode="Markdown",
    )
    return BK_IMPORT_CONFIRM


async def bk_import_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "restore":
        await update.message.reply_text("برای ادامه فقط RESTORE ارسال کنید.", parse_mode="Markdown")
        return BK_IMPORT_CONFIRM

    panel = context.user_data.get("bk_target_panel")
    local_file = context.user_data.get("bk_local_file")

    if not panel or not local_file or not os.path.exists(local_file):
        context.user_data.clear()
        await update.message.reply_text("❌ فایل یا اطلاعات پنل موجود نیست.", reply_markup=kb_main())
        return ConversationHandler.END

    await update.message.reply_text("⏳ در حال Restore بکاپ...")

    db_path = await find_db_path(panel)
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
        def sftp_upload_and_restore():
            c = ssh_client(panel["ssh_host"], panel["ssh_port"], panel["ssh_user"], panel["ssh_pass"])
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
            code, out, err = ssh_exec_raw(c, cmd, read_timeout=50)
            c.close()
            return code, out, err

        code, out, err = await asyncio.to_thread(sftp_upload_and_restore)
        if code != 0:
            raise RuntimeError(_short((out + "\n" + err).strip(), 3500))

        await restart_xui(panel)
        await update.message.reply_text(
            "✅ بکاپ با موفقیت بازیابی شد.\n\n" f"📌 بکاپ قبلی (برای اطمینان) ذخیره شد:\n{remote_backup_old}",
            parse_mode="Markdown",
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


# -------- Import new server (no save) --------
async def bk_import_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data["new_ssh"] = {}
    await q.edit_message_text("➕ **سرور جدید (بدون ذخیره)**\n\n🌐 SSH Host را ارسال کنید:", parse_mode="Markdown")
    return BK_IMPORT_NEW_SSH_HOST


async def bk_new_ssh_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["host"] = update.message.text.strip()
    await update.message.reply_text("👤 SSH Username را ارسال کنید:")
    return BK_IMPORT_NEW_SSH_USER


async def bk_new_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["user"] = update.message.text.strip()
    await update.message.reply_text("🔢 SSH Port را ارسال کنید (مثلاً 22):")
    return BK_IMPORT_NEW_SSH_PORT


async def bk_new_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        p = int(update.message.text.strip())
        if not (1 <= p <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت معتبر ارسال کنید (1..65535).")
        return BK_IMPORT_NEW_SSH_PORT
    context.user_data["new_ssh"]["port"] = p
    await update.message.reply_text("🔑 SSH Password را ارسال کنید:")
    return BK_IMPORT_NEW_SSH_PASS


async def bk_new_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["pass"] = update.message.text.strip()
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

    await update.message.reply_text(
        "⚠️ **هشدار مهم**\n\n"
        "این عملیات دیتابیس فعلی سرور را جایگزین می‌کند.\n"
        "برای ادامه RESTORE ارسال کنید:",
        parse_mode="Markdown",
    )
    return BK_IMPORT_NEW_CONFIRM


async def bk_new_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "restore":
        await update.message.reply_text("برای ادامه فقط RESTORE ارسال کنید.", parse_mode="Markdown")
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

    panel = {
        "ssh_host": ns["host"],
        "ssh_user": ns["user"],
        "ssh_port": ns["port"],
        "ssh_pass": ns["pass"],
        "panel_host": ns["host"],
    }

    await update.message.reply_text("⏳ در حال Restore روی سرور جدید...")

    db_path = await find_db_path(panel)
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
            c = ssh_client(panel["ssh_host"], panel["ssh_port"], panel["ssh_user"], panel["ssh_pass"])
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
            code, out, err = ssh_exec_raw(c, cmd, read_timeout=50)
            c.close()
            return code, out, err

        code, out, err = await asyncio.to_thread(sftp_upload_and_restore_new)
        if code != 0:
            raise RuntimeError(_short((out + "\n" + err).strip(), 3500))

        await restart_xui(panel)
        await update.message.reply_text(
            "✅ بکاپ با موفقیت بازیابی شد.\n\n"
            f"📌 بکاپ قبلی ذخیره شد:\n{remote_backup_old}\n\n"
            "ℹ️ هیچ اطلاعاتی از این سرور ذخیره نشد و همه اطلاعات موقت پاک شد.",
            parse_mode="Markdown",
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


# ------------------------- Backup menu router -------------------------
async def backup_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "bk_export":
        return await bk_export_start(update, context)
    if q.data == "bk_import":
        return await bk_import_start(update, context)
    if q.data == "bk_import_existing":
        return await bk_import_existing_choose(update, context)
    if q.data == "bk_import_new":
        return await bk_import_new_start(update, context)
    if q.data.startswith("bk_export_panel:"):
        return await bk_export_pick_panel(update, context)
    if q.data.startswith("bk_import_panel:"):
        return await bk_import_pick_panel(update, context)
    if q.data == "backup_menu":
        return await backup_menu_entry(update, context)
    return BK_MENU


# ------------------------- Global error handler -------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
    # اگر update موجود بود، به کاربر پیام بده
    try:
        if isinstance(update, Update):
            await report_error(update, "خطای داخلی ربات", "یک خطای غیرمنتظره رخ داد. دوباره تلاش کنید.")
    except Exception:
        pass


# ------------------------- Main -------------------------
def main():
    token = env_required("TOKEN")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))

    # Conversations FIRST
    conv_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_panel_entry, pattern="^add_panel$")],
        states={
            ADD_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_ip)],
            ADD_HTTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_http)],
            ADD_PANEL_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_panel_port)],
            ADD_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_path)],
            ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_user)],
            ADD_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_pass)],
            ADD_SSH_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_ssh_host)],
            ADD_SSH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_ssh_user)],
            ADD_SSH_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_ssh_port)],
            ADD_SSH_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_ssh_pass)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_add)

    conv_merge = ConversationHandler(
        entry_points=[CallbackQueryHandler(merge_entry, pattern=r"^merge:")],
        states={
            MERGE_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, merge_count)],
            MERGE_PORTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, merge_ports)],
            MERGE_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, merge_target)],
            MERGE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, merge_confirm)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_merge)

    conv_backup = ConversationHandler(
        entry_points=[CallbackQueryHandler(backup_menu_entry, pattern="^backup_menu$")],
        states={
            BK_MENU: [CallbackQueryHandler(backup_menu_router)],
            BK_EXPORT_PICK_PANEL: [CallbackQueryHandler(backup_menu_router)],
            BK_IMPORT_CHOOSE_MODE: [CallbackQueryHandler(backup_menu_router)],
            BK_IMPORT_PICK_PANEL: [CallbackQueryHandler(backup_menu_router)],
            BK_IMPORT_UPLOAD_FILE: [MessageHandler(filters.Document.ALL, bk_import_receive_file)],
            BK_IMPORT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_import_confirm)],
            BK_IMPORT_NEW_SSH_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_ssh_host)],
            BK_IMPORT_NEW_SSH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_ssh_user)],
            BK_IMPORT_NEW_SSH_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_ssh_port)],
            BK_IMPORT_NEW_SSH_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_ssh_pass)],
            BK_IMPORT_NEW_UPLOAD_FILE: [MessageHandler(filters.Document.ALL, bk_new_receive_file)],
            BK_IMPORT_NEW_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_confirm)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_backup)

    conv_edit = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^[a-zA-Z_]+=") & ~filters.COMMAND, edit_value)],
        states={EDIT_VALUE: [MessageHandler(filters.Regex(r"^[a-zA-Z_]+=") & ~filters.COMMAND, edit_value)]},
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_edit)

    # Navigation LAST + pattern دقیق
    app.add_handler(
        CallbackQueryHandler(
            nav_callbacks,
            pattern=r"^(back_main|manage_panels|start_merge|profile|panel:.*|del:.*|edit:.*)$",
        )
    )

    app.add_error_handler(on_error)

    app.run_polling()


if __name__ == "__main__":
    main()
