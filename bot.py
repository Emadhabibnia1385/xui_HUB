# bot.py — xui_HUB (Telegram Bot)
# Features:
# ✅ مدیریت سرورها (افزودن/ویرایش/حذف) + (اختیاری) ثبت اطلاعات پنل x-ui/3x-ui روی همان سرور
# ✅ ادغام کلاینت‌ها بین چند پورت به یک پورت مقصد (از طریق SSH و دیتابیس x-ui)
# ✅ مدیریت بکاپ (گرفتن بکاپ / وارد کردن بکاپ) برای سرورهای ثبت‌شده (و وارد کردن روی سرور جدید بدون ذخیره)
# ✅ نصب و پیکربندی (لیست عملیات‌ها با تیک ✅ و اجرای انتخابی) برای سرورهای ثبت‌شده یا سرور جدید بدون ذخیره
#
# Notes:
# - فقط اطلاعات "سرورها" ذخیره می‌شود (store.json)
# - هر عملیات سرور جدید (بدون ذخیره) بعد از اتمام پاک می‌شود.
# - برای جلوگیری از خطای State: range بزرگ گذاشته شده.
#
# ENV:
#   TOKEN=<telegram bot token>

import os
import json
import re
import asyncio
import tempfile
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
    # Supports both schema styles:
    # - clients table exists -> insert clients by uuid
    # - else -> merge clients inside inbound settings JSON
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
    "xui_HUB یک ربات حرفه‌ای برای **مدیریت سرورها** و (در صورت نیاز) ثبت و مدیریت پنل **3x-ui / x-ui** است.\n\n"
    "از داخل تلگرام می‌توانید:\n"
    "• سرورهای خود را اضافه/ویرایش/حذف کنید\n"
    "• پورت‌ها و کانفیگ‌ها را مدیریت کنید (ادغام کلاینت‌ها)\n"
    "• بکاپ بگیرید یا بکاپ را وارد کنید\n"
    "• عملیات نصب و پیکربندی را روی سرور اجرا کنید\n\n"
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
        [InlineKeyboardButton("🧰 نصب و پیکربندی", callback_data="setup_menu")],
        [InlineKeyboardButton("👤 پروفایل", callback_data="profile")],
    ])

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت به منو", callback_data="back_main")]])

def kb_yes_no(prefix_yes: str, prefix_no: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ بله", callback_data=prefix_yes),
            InlineKeyboardButton("❌ خیر", callback_data=prefix_no),
        ]
    ])

def kb_http_https() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔒 HTTPS", callback_data="scheme:https"),
            InlineKeyboardButton("🌐 HTTP", callback_data="scheme:http"),
        ]
    ])

def display_server_name(s: Dict[str, Any]) -> str:
    # show domain if exists else ssh_host
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

def kb_setup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 سرورهای موجود", callback_data="setup_existing")],
        [InlineKeyboardButton("➕ سرور جدید (بدون ذخیره)", callback_data="setup_new")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")],
    ])

def kb_setup_actions(selected: Set[str]) -> InlineKeyboardMarkup:
    actions = [
        ("a1", "1) آپدیت و آپگرید سیستم"),
        ("a2", "2) نصب 3x-ui (ثنایی) آخرین نسخه"),
        ("a3", "3) نصب vpanel"),
        ("a4", "4) نصب پیش‌نیازها + تونل‌ها (Azumi)"),
    ]
    rows = []
    for aid, title in actions:
        mark = "✅" if aid in selected else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {title}", callback_data=f"toggle:{aid}")])
    rows.append([InlineKeyboardButton("▶️ اجرای عملیات‌های انتخاب‌شده", callback_data="setup_run")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="setup_menu")])
    return InlineKeyboardMarkup(rows)

# ---------------- States ----------------
(
    # add server flow
    ADD_SRV_HOST, ADD_SRV_SSH_USER, ADD_SRV_SSH_PASS, ADD_SRV_SSH_PORT,
    ADD_SRV_HAS_PANEL, ADD_SRV_PANEL_HOST, ADD_SRV_PANEL_PORT, ADD_SRV_PANEL_PATH, ADD_SRV_PANEL_SCHEME,

    # edit server (field=value)
    EDIT_SERVER_FIELD,

    # merge flow
    MERGE_PICK_SERVER, MERGE_COUNT, MERGE_PORTS, MERGE_TARGET, MERGE_CONFIRM,

    # backup flow
    BK_MENU, BK_EXPORT_PICK, BK_IMPORT_MODE, BK_IMPORT_PICK, BK_IMPORT_UPLOAD, BK_IMPORT_CONFIRM,
    BK_NEW_SSH_HOST, BK_NEW_SSH_USER, BK_NEW_SSH_PASS, BK_NEW_SSH_PORT, BK_NEW_UPLOAD, BK_NEW_CONFIRM,

    # setup flow
    SETUP_MODE, SETUP_PICK, SETUP_NEW_HOST, SETUP_NEW_USER, SETUP_NEW_PASS, SETUP_NEW_PORT, SETUP_ACTIONS,
) = range(200)

# ---------------- Helper: build ssh dict from stored server ----------------
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

# ---------------- Navigation callbacks ----------------
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
            f"{one_line_hint('فقط اطلاعات سرورها ذخیره می‌شود و عملیات‌های سرور جدید (بدون ذخیره) بعد از پایان پاک می‌شوند.')}",
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
            f"{one_line_hint('این عملیات از طریق SSH انجام می‌شود. برای گرفتن بکاپ، سرور باید x-ui داشته باشد.')}",
            reply_markup=kb_backup_menu(),
            parse_mode="Markdown"
        )
        return

    if q.data == "setup_menu":
        await q.edit_message_text(
            "🧰 **نصب و پیکربندی**\n\n"
            "در این بخش می‌توانید عملیات‌های آماده را روی یک سرور اجرا کنید.\n"
            "لطفاً نوع سرور را انتخاب کنید:",
            reply_markup=kb_setup_menu(),
            parse_mode="Markdown"
        )
        return

    if q.data == "profile":
        u = update.effective_user
        username = f"@{u.username}" if u.username else "ندارد"
        servers_count = len(bucket.get("order", []))
        server_list = "\n".join([f"• {display_server_name(bucket['servers'].get(sid, {}))}" for sid in bucket.get("order", [])]) if servers_count else "—"
        text = (
            "👤 **پروفایل شما**\n\n"
            f"نام: {u.full_name}\n"
            f"یوزرنیم: {username}\n"
            f"User ID: {u.id}\n\n"
            f"تعداد سرورها: {servers_count}\n"
            f"لیست سرورها:\n{server_list}\n\n"
            "👨‍💻 Developer: @EmadHabibnia"
        )
        await q.edit_message_text(text, reply_markup=kb_back_main(), parse_mode="Markdown")
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
        return

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
    return ADD_SRV_HOST

async def add_srv_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    host = update.message.text.strip()
    context.user_data["new_server"]["ssh_host"] = host

    await update.message.reply_text(
        "👤 **نام کاربری SSH** را ارسال کنید.\n"
        f"{one_line_hint('پیش‌فرض: root — اگر همین است، دستور /skip را بزنید.')}",
        parse_mode="Markdown"
    )
    return ADD_SRV_SSH_USER

async def add_srv_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = "root" if text == SKIP_CMD else text
    context.user_data["new_server"]["ssh_user"] = user

    await update.message.reply_text(
        "🔑 **رمز عبور SSH** را ارسال کنید.\n"
        f"{one_line_hint('این اطلاعات فقط برای اتصال استفاده می‌شود.')}",
        parse_mode="Markdown"
    )
    return ADD_SRV_SSH_PASS

async def add_srv_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["ssh_pass"] = update.message.text.strip()

    await update.message.reply_text(
        "🔢 **پورت SSH** را ارسال کنید.\n"
        f"{one_line_hint('پیش‌فرض: 22 — اگر همین است، دستور /skip را بزنید.')}",
        parse_mode="Markdown"
    )
    return ADD_SRV_SSH_PORT

async def add_srv_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == SKIP_CMD:
        port = 22
    else:
        try:
            port = int(text)
            if not (1 <= port <= 65535):
                raise ValueError()
        except:
            await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
            return ADD_SRV_SSH_PORT

    context.user_data["new_server"]["ssh_port"] = port

    await update.message.reply_text(
        "✅ اتصال SSH این سرور ثبت شد.\n\n"
        "❓ آیا می‌خواهید **اطلاعات پنل x-ui / 3x-ui** همین سرور را هم اضافه کنید؟\n"
        f"{one_line_hint('اگر پنل ندارید یا نمی‌خواهید، خیر را بزنید.')}",
        reply_markup=kb_yes_no("srv_has_panel_yes", "srv_has_panel_no"),
        parse_mode="Markdown"
    )
    return ADD_SRV_HAS_PANEL

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
    return ADD_SRV_PANEL_HOST

async def add_srv_has_panel_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # finalize without panel
    await finalize_new_server(q, context, include_panel=False)
    return ConversationHandler.END

async def add_srv_panel_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    default = context.user_data["new_server"]["panel"].get("panel_host_default") or context.user_data["new_server"]["ssh_host"]
    host = default if text == SKIP_CMD else text
    context.user_data["new_server"]["panel"]["panel_host"] = host

    await update.message.reply_text(
        "🔢 **پورت پنل** را ارسال کنید.\n"
        f"{one_line_hint('مثال: 2053 یا 54321 (همان پورتی که پنل روی آن اجرا می‌شود).')}",
        parse_mode="Markdown"
    )
    return ADD_SRV_PANEL_PORT

async def add_srv_panel_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
        return ADD_SRV_PANEL_PORT
    context.user_data["new_server"]["panel"]["panel_port"] = port

    await update.message.reply_text(
        "🧭 **URI Path پنل** را ارسال کنید.\n"
        f"{one_line_hint('اگر پنل path ندارد، /skip بزنید تا مقدار پیش‌فرض / قرار بگیرد.')}",
        parse_mode="Markdown"
    )
    return ADD_SRV_PANEL_PATH

async def add_srv_panel_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    path = "/" if text == SKIP_CMD else text
    if not path.startswith("/"):
        path = "/" + path
    context.user_data["new_server"]["panel"]["panel_path"] = path

    await update.message.reply_text(
        "🔒 **نوع اتصال پنل** را انتخاب کنید:",
        reply_markup=kb_http_https(),
        parse_mode="Markdown"
    )
    return ADD_SRV_PANEL_SCHEME

async def add_srv_panel_scheme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    scheme = q.data.split(":", 1)[1].strip()
    if scheme not in ("http", "https"):
        await q.edit_message_text("گزینه نامعتبر. دوباره انتخاب کنید.", reply_markup=kb_http_https())
        return ADD_SRV_PANEL_SCHEME

    context.user_data["new_server"]["panel"]["panel_scheme"] = scheme

    await finalize_new_server(q, context, include_panel=True)
    return ConversationHandler.END

async def finalize_new_server(q_or_msg, context: ContextTypes.DEFAULT_TYPE, include_panel: bool):
    store = load_store()
    uid = context._user_id_and_data[0] if hasattr(context, "_user_id_and_data") else None  # fallback not used
    user_id = None
    # safer: take from update via q_or_msg if possible
    try:
        user_id = q_or_msg.from_user.id
    except:
        try:
            user_id = q_or_msg.message.from_user.id
        except:
            user_id = None

    if user_id is None:
        # last fallback
        user_id = context.user_data.get("_uid") or 0

    bucket = get_user_bucket(store, user_id)

    srv = context.user_data.get("new_server") or {}
    if not include_panel:
        srv["panel"] = {}

    # create unique id
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

    # q_or_msg could be a CallbackQuery or Message
    if hasattr(q_or_msg, "edit_message_text"):
        await q_or_msg.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb_main())
    else:
        await q_or_msg.reply_text(msg, parse_mode="Markdown", reply_markup=kb_main())

# ---------------- Edit Server Flow (field=value) ----------------
async def edit_server_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = context.user_data.get("edit_sid")
    if not sid:
        return ConversationHandler.END

    text = update.message.text.strip()
    if "=" not in text:
        await update.message.reply_text("فرمت صحیح: `field=value`", parse_mode="Markdown")
        return EDIT_SERVER_FIELD

    key, val = text.split("=", 1)
    key = key.strip()
    val = val.strip()

    allowed = {
        "ssh_host", "ssh_user", "ssh_pass", "ssh_port",
        "panel_host", "panel_port", "panel_path", "panel_scheme",
    }
    if key not in allowed:
        await update.message.reply_text("❌ نام فیلد معتبر نیست. دوباره ارسال کنید.")
        return EDIT_SERVER_FIELD

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
            return EDIT_SERVER_FIELD

    if key == "panel_scheme":
        vv = val.lower()
        if vv not in ("http", "https"):
            await update.message.reply_text("❌ فقط http یا https")
            return EDIT_SERVER_FIELD
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
        "⚠️ نکته مهم:\n"
        "• پورت مقصد را از قبل داخل پنل ساخته باشید.\n"
        "• این عملیات از طریق SSH و دیتابیس x-ui انجام می‌شود.\n\n"
        "✅ ابتدا **تعداد پورت‌های ورودی** را ارسال کنید (مثلاً 2):",
        parse_mode="Markdown"
    )
    return MERGE_COUNT

async def merge_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if not (1 <= n <= 30):
            raise ValueError()
    except:
        await update.message.reply_text("❌ عدد معتبر (1 تا 30) ارسال کنید.")
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
    except:
        await update.message.reply_text("❌ پورت معتبر ارسال کنید.")
        return MERGE_PORTS

    m["ports"].append(port)
    idx = len(m["ports"])
    if idx < m["count"]:
        await update.message.reply_text(f"✅ ثبت شد. پورت بعدی (پورت {idx+1}):")
        return MERGE_PORTS

    await update.message.reply_text("✅ همه ورودی‌ها ثبت شد. حالا **پورت مقصد** را ارسال کنید (مثلاً 443):")
    return MERGE_TARGET

async def merge_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("❌ پورت مقصد معتبر ارسال کنید.")
        return MERGE_TARGET

    m["target_port"] = port
    await update.message.reply_text(
        "🧾 **خلاصه عملیات**\n\n"
        f"ورودی‌ها: `{m['ports']}`\n"
        f"مقصد: `{m['target_port']}`\n\n"
        "اگر آماده‌اید برای اجرا عبارت زیر را ارسال کنید:\n"
        "`OK`",
        parse_mode="Markdown"
    )
    return MERGE_CONFIRM

async def merge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "ok":
        await update.message.reply_text("برای ادامه فقط `OK` ارسال کنید.", parse_mode="Markdown")
        return MERGE_CONFIRM

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

# ---------------- Backup Flow ----------------
async def backup_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🗂 **مدیریت بکاپ**\n\n"
        "• 📤 گرفتن بکاپ: بکاپ دیتابیس x-ui همین لحظه دریافت می‌شود.\n"
        "• 📥 وارد کردن بکاپ: بازیابی دیتابیس از فایل بکاپ.\n\n"
        f"{one_line_hint('این عملیات از طریق SSH انجام می‌شود.')}",
        reply_markup=kb_backup_menu(),
        parse_mode="Markdown"
    )
    return BK_MENU

async def bk_export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)
    if not bucket["order"]:
        await q.edit_message_text("اول یک سرور اضافه کنید.", reply_markup=kb_servers_list(store, uid))
        return ConversationHandler.END

    rows = []
    for sid in bucket["order"]:
        srv = bucket["servers"].get(sid, {})
        rows.append([InlineKeyboardButton(f"📤 {display_server_name(srv)}", callback_data=f"bk_export:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])

    await q.edit_message_text("📤 سرور موردنظر برای بکاپ را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
    return BK_EXPORT_PICK

async def bk_export_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    sid = q.data.split(":", 1)[1]
    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)
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

async def bk_import_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text(
        "📥 **وارد کردن بکاپ (Restore)**\n\n"
        "روش بازیابی را انتخاب کنید:",
        reply_markup=kb_backup_import_mode(),
        parse_mode="Markdown"
    )
    return BK_IMPORT_MODE

async def bk_import_existing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)
    if not bucket["order"]:
        await q.edit_message_text("اول یک سرور اضافه کنید.", reply_markup=kb_servers_list(store, uid))
        return ConversationHandler.END

    rows = []
    for sid in bucket["order"]:
        srv = bucket["servers"].get(sid, {})
        rows.append([InlineKeyboardButton(f"🔁 {display_server_name(srv)}", callback_data=f"bk_import_pick:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])

    await q.edit_message_text(
        "🔁 سرور مقصد برای Restore را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return BK_IMPORT_PICK

async def bk_import_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    sid = q.data.split(":", 1)[1]
    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["bk_target"] = {"mode": "existing", "sid": sid}
    await q.edit_message_text(
        "📎 لطفاً **فایل بکاپ دیتابیس** را ارسال کنید (فایل `.db`).\n\n"
        "⚠️ این عملیات دیتابیس فعلی را جایگزین می‌کند.",
        parse_mode="Markdown"
    )
    return BK_IMPORT_UPLOAD

async def bk_import_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data["bk_target"] = {"mode": "new", "ssh": {}}

    await q.edit_message_text(
        "➕ **سرور جدید (بدون ذخیره)**\n\n"
        "🌐 لطفاً **IP یا دامنه سرور** را ارسال کنید.\n"
        f"{one_line_hint('این آدرس برای SSH استفاده می‌شود.')}",
        parse_mode="Markdown"
    )
    return BK_NEW_SSH_HOST

async def bk_new_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bk_target"]["ssh"]["ssh_host"] = update.message.text.strip()
    await update.message.reply_text(
        "👤 **نام کاربری SSH** را ارسال کنید.\n"
        f"{one_line_hint('پیش‌فرض: root — اگر همین است /skip بزنید.')}",
        parse_mode="Markdown"
    )
    return BK_NEW_SSH_USER

async def bk_new_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    context.user_data["bk_target"]["ssh"]["ssh_user"] = "root" if txt == SKIP_CMD else txt
    await update.message.reply_text(
        "🔑 **رمز عبور SSH** را ارسال کنید.",
        parse_mode="Markdown"
    )
    return BK_NEW_SSH_PASS

async def bk_new_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bk_target"]["ssh"]["ssh_pass"] = update.message.text.strip()
    await update.message.reply_text(
        "🔢 **پورت SSH** را ارسال کنید.\n"
        f"{one_line_hint('پیش‌فرض: 22 — اگر همین است /skip بزنید.')}",
        parse_mode="Markdown"
    )
    return BK_NEW_SSH_PORT

async def bk_new_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == SKIP_CMD:
        p = 22
    else:
        try:
            p = int(txt)
            if not (1 <= p <= 65535):
                raise ValueError()
        except:
            await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
            return BK_NEW_SSH_PORT
    context.user_data["bk_target"]["ssh"]["ssh_port"] = p

    await update.message.reply_text(
        "📎 حالا **فایل بکاپ دیتابیس** (`.db`) را ارسال کنید:",
        parse_mode="Markdown"
    )
    return BK_NEW_UPLOAD

async def bk_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("فایل را به صورت Document ارسال کنید.")
        return BK_IMPORT_UPLOAD

    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(prefix="xuihub_restore_", suffix=".db", delete=False) as f:
        local_path = f.name
    await tg_file.download_to_drive(custom_path=local_path)
    context.user_data["bk_local_file"] = local_path

    await update.message.reply_text(
        "⚠️ **هشدار مهم**\n\n"
        "این عملیات دیتابیس فعلی را به‌طور کامل جایگزین می‌کند.\n"
        "اگر مطمئن هستید عبارت زیر را ارسال کنید:\n"
        "`RESTORE`",
        parse_mode="Markdown"
    )
    return BK_IMPORT_CONFIRM

async def bk_receive_file_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # same as bk_receive_file but state differs
    return await bk_receive_file(update, context)

async def bk_confirm_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "restore":
        await update.message.reply_text("برای ادامه فقط `RESTORE` ارسال کنید.", parse_mode="Markdown")
        return BK_IMPORT_CONFIRM

    target = context.user_data.get("bk_target") or {}
    local_file = context.user_data.get("bk_local_file")
    if not local_file or not os.path.exists(local_file):
        context.user_data.clear()
        await update.message.reply_text("❌ فایل بکاپ موجود نیست.", reply_markup=kb_main())
        return ConversationHandler.END

    # build ssh
    if target.get("mode") == "existing":
        store = load_store()
        uid = update.effective_user.id
        bucket = get_user_bucket(store, uid)
        srv = bucket["servers"].get(target.get("sid", ""))
        if not srv:
            context.user_data.clear()
            await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
            return ConversationHandler.END
        ssh = ssh_from_server(srv)
        srv_label = display_server_name(srv)
        keep_data = True
    else:
        ssh = target.get("ssh") or {}
        srv_label = ssh.get("ssh_host", "server")
        keep_data = False

    await update.message.reply_text("⏳ در حال Restore بکاپ...")

    db_path = await find_db_path(ssh)
    if not db_path:
        try: os.remove(local_file)
        except: pass
        context.user_data.clear()
        await update.message.reply_text(
            "❌ دیتابیس پیدا نشد یا sudo ندارم.\n"
            f"{one_line_hint('برای Restore باید x-ui نصب باشد و کاربر SSH دسترسی sudo داشته باشد.')}",
            reply_markup=kb_main()
        )
        return ConversationHandler.END

    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%d_%H%M")
    remote_upload = f"/tmp/xuihub_restore_upload_{ts}.db"
    remote_backup_old = f"/tmp/xuihub_old_before_restore_{ts}.db"

    try:
        def sftp_upload_and_restore():
            c = ssh_client(ssh["ssh_host"], int(ssh["ssh_port"]), ssh["ssh_user"], ssh["ssh_pass"])
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
            code, out, err = ssh_exec_raw(c, cmd)
            c.close()
            return code, out, err

        code, out, err = await asyncio.to_thread(sftp_upload_and_restore)
        if code != 0:
            raise RuntimeError((out + "\n" + err).strip()[:3500])

        await restart_xui(ssh)

        extra = "\n\nℹ️ هیچ اطلاعاتی ذخیره نشد و داده‌های موقت پاک شد." if not keep_data else ""
        await update.message.reply_text(
            "✅ بکاپ با موفقیت بازیابی شد.\n\n"
            f"🖥 سرور: `{srv_label}`\n"
            f"📌 بکاپ قبلی (جهت اطمینان) روی سرور ذخیره شد:\n`{remote_backup_old}`"
            f"{extra}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در Restore:\n{e}")
    finally:
        try: os.remove(local_file)
        except: pass
        context.user_data.clear()

    await update.message.reply_text("برای ادامه از منوی اصلی استفاده کنید 👇", reply_markup=kb_main())
    return ConversationHandler.END

# ---------------- Setup Flow (Install & Configure) ----------------
def parse_3xui_creds(output: str) -> Optional[Tuple[str, str]]:
    user = None
    pw = None
    for line in output.splitlines():
        l = line.strip()
        if re.search(r"(username|user name|user)\s*[:：]", l, re.I):
            user = l.split(":", 1)[-1].strip()
        if re.search(r"(password|pass)\s*[:：]", l, re.I):
            pw = l.split(":", 1)[-1].strip()
    if user and pw:
        return user, pw
    u = re.search(r"(username|user name|user)\s*[:：]\s*([^\s]+)", output, re.I)
    p = re.search(r"(password|pass)\s*[:：]\s*([^\s]+)", output, re.I)
    if u and p:
        return u.group(2), p.group(2)
    return None

async def setup_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("setup", None)

    await q.edit_message_text(
        "🧰 **نصب و پیکربندی**\n\n"
        "نوع سرور را انتخاب کنید:",
        reply_markup=kb_setup_menu(),
        parse_mode="Markdown"
    )
    return SETUP_MODE

async def setup_existing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)
    if not bucket["order"]:
        await q.edit_message_text("هیچ سروری ثبت نشده. ابتدا سرور اضافه کنید.", reply_markup=kb_servers_list(store, uid))
        return ConversationHandler.END

    context.user_data["setup"] = {"mode": "existing", "selected": set(), "ssh": {}}
    rows = []
    for sid in bucket["order"]:
        srv = bucket["servers"].get(sid, {})
        rows.append([InlineKeyboardButton(f"🖥 {display_server_name(srv)}", callback_data=f"setup_pick:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="setup_menu")])

    await q.edit_message_text("🔁 یک سرور را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
    return SETUP_PICK

async def setup_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = q.data.split(":", 1)[1]

    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    context.user_data["setup"]["ssh"] = ssh_from_server(srv)
    context.user_data["setup"]["label"] = display_server_name(srv)

    await q.edit_message_text(
        f"✅ سرور انتخاب شد: **{context.user_data['setup']['label']}**\n\n"
        "حالا عملیات‌های موردنظر را تیک بزنید و سپس ▶️ اجرا را بزنید:",
        reply_markup=kb_setup_actions(context.user_data["setup"]["selected"]),
        parse_mode="Markdown"
    )
    return SETUP_ACTIONS

async def setup_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data["setup"] = {"mode": "new", "selected": set(), "ssh": {}, "label": ""}
    await q.edit_message_text(
        "➕ **سرور جدید (بدون ذخیره اطلاعات)**\n\n"
        "🌐 لطفاً **IP یا دامنه سرور** را ارسال کنید:",
        parse_mode="Markdown"
    )
    return SETUP_NEW_HOST

async def setup_new_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["setup"]["ssh"]["ssh_host"] = update.message.text.strip()
    await update.message.reply_text(
        "👤 **نام کاربری SSH** را ارسال کنید.\n"
        f"{one_line_hint('پیش‌فرض: root — اگر همین است /skip بزنید.')}",
        parse_mode="Markdown"
    )
    return SETUP_NEW_USER

async def setup_new_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    context.user_data["setup"]["ssh"]["ssh_user"] = "root" if txt == SKIP_CMD else txt
    await update.message.reply_text("🔑 **رمز عبور SSH** را ارسال کنید:", parse_mode="Markdown")
    return SETUP_NEW_PASS

async def setup_new_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["setup"]["ssh"]["ssh_pass"] = update.message.text.strip()
    await update.message.reply_text(
        "🔢 **پورت SSH** را ارسال کنید.\n"
        f"{one_line_hint('پیش‌فرض: 22 — اگر همین است /skip بزنید.')}",
        parse_mode="Markdown"
    )
    return SETUP_NEW_PORT

async def setup_new_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == SKIP_CMD:
        p = 22
    else:
        try:
            p = int(txt)
            if not (1 <= p <= 65535):
                raise ValueError()
        except:
            await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
            return SETUP_NEW_PORT

    context.user_data["setup"]["ssh"]["ssh_port"] = p
    context.user_data["setup"]["label"] = context.user_data["setup"]["ssh"]["ssh_host"]

    await update.message.reply_text(
        "✅ اتصال آماده است.\n\n"
        "حالا عملیات‌های موردنظر را تیک بزنید و سپس ▶️ اجرا را بزنید:",
        reply_markup=kb_setup_actions(context.user_data["setup"]["selected"])
    )
    return SETUP_ACTIONS

async def setup_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "setup_menu":
        return await setup_menu_entry(update, context)

    setup = context.user_data.get("setup")
    if not setup:
        await q.edit_message_text("جلسه منقضی شد. دوباره وارد شوید.", reply_markup=kb_main())
        return ConversationHandler.END

    if q.data.startswith("toggle:"):
        aid = q.data.split(":", 1)[1]
        if aid in setup["selected"]:
            setup["selected"].remove(aid)
        else:
            setup["selected"].add(aid)
        await q.edit_message_reply_markup(reply_markup=kb_setup_actions(setup["selected"]))
        return SETUP_ACTIONS

    if q.data != "setup_run":
        return SETUP_ACTIONS

    if not setup["selected"]:
        await q.edit_message_text("هیچ عملیاتی انتخاب نشده. حداقل یکی را تیک بزنید ✅", reply_markup=kb_setup_actions(setup["selected"]))
        return SETUP_ACTIONS

    ssh = setup["ssh"]
    label = setup.get("label") or ssh.get("ssh_host", "server")
    await q.edit_message_text("⏳ در حال اجرای عملیات‌ها...")

    results: List[str] = []
    full_output = ""

    async def run_step(title: str, cmd: str):
        nonlocal full_output
        code, out, err = await ssh_run_cmd(ssh, cmd)
        full_output += "\n" + (out or "") + "\n" + (err or "")
        if code == 0:
            results.append(f"✅ {title}")
        else:
            snippet = (out + "\n" + err).strip()[:1400]
            results.append(f"❌ {title}\n{snippet}")

    if "a1" in setup["selected"]:
        await run_step("آپدیت و آپگرید سیستم", "sudo apt-get update -y && sudo apt-get upgrade -y")

    if "a2" in setup["selected"]:
        await run_step("نصب 3x-ui (ثنایی) آخرین نسخه", "bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)")

    if "a3" in setup["selected"]:
        host_for_ip = ssh.get("ssh_host", "")
        vpanel_cmd = f"""
set -e
cd /tmp
wget -O vpanel-installer.sh https://raw.githubusercontent.com/vpaneladmin/vpanel-bash/main/vpanel-installer.sh
chmod +x vpanel-installer.sh
printf "%s\\n" "{host_for_ip}" | sudo ./vpanel-installer.sh
"""
        await run_step("نصب vpanel", vpanel_cmd)

    if "a4" in setup["selected"]:
        prereq = r"""
set -e
sudo apt update -y
sudo apt install -y python3 python3-pip curl wget
pip3 install --upgrade pip
pip3 install netifaces colorama requests
"""
        tunnel_6to4 = r"""
set -e
python3 <(curl -Ls https://raw.githubusercontent.com/Azumi67/6TO4-GRE-IPIP-SIT/main/ipipv2.py) --ipv4
"""
        backhaul = r"""
set -e
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Azumi67/Backhaul_script/refs/heads/main/backhaul.sh)"
"""
        await run_step("نصب پیش‌نیازها", prereq)
        await run_step("اجرای تونل 6TO4/GRE/... (Azumi)", tunnel_6to4)
        await run_step("اجرای Backhaul (Azumi)", backhaul)

    report = "🧰 **گزارش عملیات‌ها**\n\n" + f"🖥 سرور: `{label}`\n\n" + "\n\n".join(results)
    await q.message.reply_text(report[:3900], parse_mode="Markdown")

    if "a2" in setup["selected"]:
        creds = parse_3xui_creds(full_output)
        if creds:
            u, p = creds
            await q.message.reply_text(
                "🔐 **اطلاعات ورود 3x-ui**\n\n"
                f"🖥 سرور: `{label}`\n"
                f"👤 Username: `{u}`\n"
                f"🔑 Password: `{p}`\n\n"
                "✅ (برای کپی، روی هرکدام لمس کنید)",
                parse_mode="Markdown"
            )
        else:
            await q.message.reply_text(
                "⚠️ نصب انجام شد، اما یوزرنیم/پسورد از خروجی نصب قابل تشخیص نبود.\n"
                "می‌توانید داخل سرور با اجرای `x-ui` یا `3x-ui` اطلاعات را ببینید.",
                parse_mode="Markdown"
            )

    if setup.get("mode") == "new":
        context.user_data.pop("setup", None)

    await q.message.reply_text("✅ تمام شد. از منوی اصلی ادامه دهید 👇", reply_markup=kb_main())
    return ConversationHandler.END

# ---------------- Router for backup callbacks inside conversation ----------------
async def backup_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "backup_menu":
        return await backup_menu_entry(update, context)
    if q.data == "bk_export":
        return await bk_export_start(update, context)
    if q.data.startswith("bk_export:"):
        return await bk_export_pick(update, context)
    if q.data == "bk_import":
        return await bk_import_start(update, context)
    if q.data == "bk_import_existing":
        return await bk_import_existing(update, context)
    if q.data.startswith("bk_import_pick:"):
        return await bk_import_pick(update, context)
    if q.data == "bk_import_new":
        return await bk_import_new(update, context)

    return BK_MENU

# ---------------- Main ----------------
def main():
    token = env_required("TOKEN")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))

    # Add Server Conversation
    conv_add_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_server_entry, pattern="^add_server$")],
        states={
            ADD_SRV_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_srv_host)],
            ADD_SRV_SSH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_srv_ssh_user)],
            ADD_SRV_SSH_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_srv_ssh_pass)],
            ADD_SRV_SSH_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_srv_ssh_port)],
            ADD_SRV_HAS_PANEL: [
                CallbackQueryHandler(add_srv_has_panel_yes, pattern="^srv_has_panel_yes$"),
                CallbackQueryHandler(add_srv_has_panel_no, pattern="^srv_has_panel_no$"),
            ],
            ADD_SRV_PANEL_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_srv_panel_host)],
            ADD_SRV_PANEL_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_srv_panel_port)],
            ADD_SRV_PANEL_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_srv_panel_path)],
            ADD_SRV_PANEL_SCHEME: [CallbackQueryHandler(add_srv_panel_scheme, pattern=r"^scheme:(http|https)$")],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_add_server)

    # Edit Server Conversation (field=value)
    conv_edit_server = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^(ssh_host|ssh_user|ssh_pass|ssh_port|panel_host|panel_port|panel_path|panel_scheme)\s*=") & ~filters.COMMAND, edit_server_field)],
        states={EDIT_SERVER_FIELD: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_server_field)]},
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_edit_server)

    # Merge Conversation
    conv_merge = ConversationHandler(
        entry_points=[CallbackQueryHandler(merge_entry, pattern=r"^merge_server:")],
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

    # Backup Conversation
    conv_backup = ConversationHandler(
        entry_points=[CallbackQueryHandler(backup_menu_entry, pattern="^backup_menu$")],
        states={
            BK_MENU: [CallbackQueryHandler(backup_router)],
            BK_EXPORT_PICK: [CallbackQueryHandler(backup_router)],
            BK_IMPORT_MODE: [CallbackQueryHandler(backup_router)],
            BK_IMPORT_PICK: [CallbackQueryHandler(backup_router)],
            BK_IMPORT_UPLOAD: [MessageHandler(filters.Document.ALL, bk_receive_file)],
            BK_IMPORT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_confirm_restore)],

            BK_NEW_SSH_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_host)],
            BK_NEW_SSH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_user)],
            BK_NEW_SSH_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_pass)],
            BK_NEW_SSH_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_new_port)],
            BK_NEW_UPLOAD: [MessageHandler(filters.Document.ALL, bk_receive_file_new)],
            BK_NEW_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bk_confirm_restore)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_backup)

    # Setup Conversation
    conv_setup = ConversationHandler(
        entry_points=[CallbackQueryHandler(setup_menu_entry, pattern="^setup_menu$")],
        states={
            SETUP_MODE: [
                CallbackQueryHandler(setup_existing, pattern="^setup_existing$"),
                CallbackQueryHandler(setup_new, pattern="^setup_new$"),
                CallbackQueryHandler(setup_menu_entry, pattern="^setup_menu$"),
            ],
            SETUP_PICK: [CallbackQueryHandler(setup_pick, pattern=r"^setup_pick:")],
            SETUP_NEW_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_new_host)],
            SETUP_NEW_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_new_user)],
            SETUP_NEW_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_new_pass)],
            SETUP_NEW_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_new_port)],
            SETUP_ACTIONS: [CallbackQueryHandler(setup_actions)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_setup)

    # Main navigation (AFTER conversations)
    async def nav_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()

        # route: manage servers
        if q.data == "manage_servers":
            return await nav(update, context)

        # route: add server
        if q.data == "add_server":
            # handled by conv_add_server entry point; ignore here
            return

        # route: edit server
        if q.data.startswith("edit_server:"):
            return await nav(update, context)

        # route: delete server
        if q.data.startswith("del_server:"):
            return await nav(update, context)

        # route: merge menu (pick server)
        if q.data == "merge_menu":
            return await nav(update, context)

        # route: backup menu
        if q.data == "backup_menu":
            # handled by conversation entry as well; but safe
            return await nav(update, context)

        # route: setup menu
        if q.data == "setup_menu":
            # handled by conversation entry as well; but safe
            return await nav(update, context)

        # other
        return await nav(update, context)

    app.add_handler(CallbackQueryHandler(nav_wrapper))

    app.run_polling()

if __name__ == "__main__":
    main()
