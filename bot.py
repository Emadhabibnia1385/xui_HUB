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

# ------------------------- .env loader -------------------------
def load_env_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                data[k] = v
    except Exception:
        pass
    return data

def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if v:
        return v
    # fallback
    env2 = load_env_file("/opt/xui_HUB/.env")
    v2 = (env2.get(name) or "").strip()
    if v2:
        return v2
    raise RuntimeError(f"Missing env: {name} (set env or /opt/xui_HUB/.env)")

# ------------------------- Storage -------------------------
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

def safe_server_id(ip: str) -> str:
    pid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", ip.strip())
    return pid or "server"

def is_ipv4(s: str) -> bool:
    s = s.strip()
    m = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", s)
    if not m:
        return False
    parts = [int(x) for x in m.groups()]
    return all(0 <= x <= 255 for x in parts)

# ------------------------- Jalali (Shamsi) -------------------------
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

# ------------------------- SSH helpers (ROBUST) -------------------------
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
        code, out, err = ssh_exec_raw(c, cmd, read_timeout=read_timeout)
    finally:
        c.close()
    return code, out, err

# ------------------------- FAST FIND DB (NO HANG) -------------------------
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
    # FIX: port ممکنه TEXT باشه، پس هم CAST و هم string را چک می‌کنیم
    return f"""sudo sqlite3 "{db_path}" "SELECT id FROM inbounds WHERE CAST(port AS INTEGER)={port} OR port='{port}' ORDER BY id DESC LIMIT 1;" """

def list_ports_cmd(db_path: str) -> str:
    return f"""sudo sqlite3 "{db_path}" "SELECT port FROM inbounds ORDER BY CAST(port AS INTEGER) ASC;" """

def debug_inbounds_tail_cmd(db_path: str) -> str:
    return f"""sudo sqlite3 "{db_path}" "SELECT id,port,remark FROM inbounds ORDER BY id DESC LIMIT 8;" """

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

# ------------------------- Text helpers -------------------------
def short(s: str, n: int = 1600) -> str:
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")

def fmt_kv(label: str, value: str) -> str:
    # فقط مقدار کپی‌پذیر باشد
    return f"{label} `{value}`"

START_TEXT = (
    "🤖 به xuiHUB خوش آمدید\n\n"
    "xuiHUB یک ربات حرفه‌ای برای مدیریت پنل‌های 3x-ui / x-ui است.\n"
    "از داخل تلگرام می‌توانید سرورها، پورت‌ها، کانفیگ‌ها و بکاپ‌ها را مدیریت کنید.\n\n"
    "برای شروع، از منوی زیر استفاده کنید 👇\n\n"
    "👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)

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

    # Ports Manager (user input only)
    PORTS_COUNT,
    PORTS_ITEMS,

    # Merge (needs SSH+DB)
    MERGE_COUNT,
    MERGE_PORTS,
    MERGE_TARGET,
    MERGE_CONFIRM,

    # Backup
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

    # Server edit (button based)
    SRV_EDIT_MENU,
    SRV_EDIT_VALUE,
) = range(31)

# ------------------------- Keyboards -------------------------
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖥 مدیریت سرورها", callback_data="servers")],
            [
                InlineKeyboardButton("🔀 مدیریت پورت و کانفیگ", callback_data="ports_menu"),
                InlineKeyboardButton("🗂 مدیریت بکاپ", callback_data="backup_menu"),
            ],
            [InlineKeyboardButton("👤 پروفایل", callback_data="profile")],
        ]
    )

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])

def kb_servers(store: Dict[str, Any], user_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(store, user_id)
    rows: List[List[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton("➕ افزودن سرور", callback_data="srv_add")])

    for sid in bucket.get("order", []):
        srv = bucket["servers"].get(sid) or {}
        ip = srv.get("ip", sid)
        panel = srv.get("panel") or {}
        dom = (panel.get("domain") or "").strip()
        text = ip if not dom else f"{ip} ({dom})"
        rows.append([InlineKeyboardButton(text, callback_data=f"srv:{sid}")])

    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_server_details_actions(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ ویرایش اطلاعات", callback_data=f"srv_edit:{sid}")],
            [InlineKeyboardButton("🗑 حذف", callback_data=f"srv_del:{sid}")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="servers")],
        ]
    )

def kb_yes_no_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ تایید", callback_data="srv_add_panel_yes"),
            InlineKeyboardButton("❌ خیر", callback_data="srv_add_panel_no"),
        ]]
    )

def kb_http_https() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🔒 HTTP", callback_data="srv_scheme:http"),
            InlineKeyboardButton("🔐 HTTPS", callback_data="srv_scheme:https"),
        ]]
    )

def kb_ports_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ ثبت لیست پورت‌ها (از کاربر)", callback_data="ports_add")],
            [InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")],
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
        f"🤖 xuiHUB\n"
        f"👨‍💻 Developer: @EmadHabibnia"
    )

async def find_db_path(server: Dict[str, Any]) -> Optional[str]:
    code, out, err = await asyncio.to_thread(
        ssh_exec,
        server["ip"],
        int(server.get("ssh_port", 22)),
        server["ssh_user"],
        server["ssh_pass"],
        FIND_DB_CMD,
    )
    db_path = out.strip().splitlines()[-1] if out.strip() else ""
    if code != 0:
        return None
    if "NOT_FOUND" in db_path or not db_path:
        return None
    return db_path

async def restart_xui(server: Dict[str, Any]) -> None:
    await asyncio.to_thread(
        ssh_exec,
        server["ip"],
        int(server.get("ssh_port", 22)),
        server["ssh_user"],
        server["ssh_pass"],
        "sudo x-ui restart || sudo systemctl restart x-ui || true",
    )

# ------------------------- Error reporter -------------------------
async def report_error(update: Update, title: str, detail: str, extra: str = ""):
    msg = f"⚠️ {title}\n\n{detail}"
    if extra.strip():
        msg += f"\n\nجزئیات:\n```text\n{short(extra, 1200)}\n```"
    if update.message:
        await update.message.reply_text(msg)
    elif update.callback_query:
        try:
            await update.callback_query.message.reply_text(msg)
        except Exception:
            pass

# ------------------------- Start/Cancel -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(START_TEXT, reply_markup=kb_main())

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ عملیات لغو شد.", reply_markup=kb_main())

# ------------------------- Navigation callbacks -------------------------
async def nav_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    if q.data == "back_main":
        context.user_data.clear()
        await q.edit_message_text(START_TEXT, reply_markup=kb_main())
        return

    if q.data == "servers":
        context.user_data.clear()
        await q.edit_message_text("🖥 مدیریت سرورها\n\nیکی از سرورها را انتخاب کنید 👇", reply_markup=kb_servers(store, user_id))
        return

    if q.data == "ports_menu":
        context.user_data.clear()
        await q.edit_message_text("🔀 مدیریت پورت و کانفیگ\n\nاین بخش فعلاً پورت‌ها را از خود شما می‌گیرد 👇", reply_markup=kb_ports_menu())
        return

    if q.data == "backup_menu":
        context.user_data.clear()
        await q.edit_message_text("🗂 مدیریت بکاپ\n\nاز اینجا می‌توانید بکاپ بگیرید یا Restore کنید 👇", reply_markup=kb_backup_menu())
        return

    if q.data == "profile":
        u = update.effective_user
        username = f"@{u.username}" if u.username else "ندارد"
        servers_count = len(bucket.get("order", []))
        text = (
            "👤 پروفایل شما\n\n"
            f"نام: {u.full_name}\n"
            f"یوزرنیم: {username}\n"
            f"User ID: {u.id}\n"
            f"تعداد سرورها: {servers_count}"
        )
        await q.edit_message_text(text, reply_markup=kb_back_main())
        return

    # Open server details
    if q.data.startswith("srv:"):
        sid = q.data.split(":", 1)[1]
        srv = bucket["servers"].get(sid)
        if not srv:
            await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_servers(store, user_id))
            return

        await q.edit_message_text("⏳ در حال اتصال و انجام عملیات...")
        try:
            db_path = await find_db_path(srv)
            panel = srv.get("panel") or {}
            ip = srv.get("ip", "")
            ssh_user = srv.get("ssh_user", "")
            ssh_pass = srv.get("ssh_pass", "")
            dom = panel.get("domain") or ""

            lines = []
            lines.append(fmt_kv("Ipv4:", ip))
            lines.append(fmt_kv("User:", ssh_user))
            lines.append(fmt_kv("Pass:", ssh_pass))
            lines.append("")

            if dom:
                lines.append(fmt_kv("Paneldomin:", dom))
                lines.append("")
                scheme = panel.get("scheme") or "http"
                pport = str(panel.get("panel_port") or "")
                ppath = panel.get("panel_path") or "/"
                if not ppath.startswith("/"):
                    ppath = "/" + ppath
                xui_url = f"{scheme}://{dom}:{pport}{ppath}" if pport else f"{scheme}://{dom}{ppath}"
                lines.append(fmt_kv("Xui:", xui_url))
                lines.append(fmt_kv("User:", str(panel.get("panel_user") or "")))
                lines.append(fmt_kv("Pass:", str(panel.get("panel_pass") or "")))
                lines.append("")
                if pport:
                    lines.append(fmt_kv("Port panel:", pport))
                lines.append("")

            if not db_path:
                lines.append("خطا: دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم")
                await q.edit_message_text("\n".join(lines), reply_markup=kb_server_details_actions(sid))
                return

            code, out, err = await asyncio.to_thread(
                ssh_exec,
                srv["ip"],
                int(srv.get("ssh_port", 22)),
                srv["ssh_user"],
                srv["ssh_pass"],
                list_ports_cmd(db_path),
            )
            if code != 0:
                lines.append("خطا: نتوانستم پورت‌ها را از دیتابیس بخوانم")
                lines.append(f"جزئیات: {short(err or out, 500)}")
                await q.edit_message_text("\n".join(lines), reply_markup=kb_server_details_actions(sid))
                return

            ports = [p.strip() for p in out.splitlines() if p.strip()]
            if ports:
                lines.append("Port ها خط به خط:")
                for p in ports[:200]:
                    lines.append(f"`{p}`")
                lines.append("")
                lines.append(f"`{','.join(ports)}`")

            await q.edit_message_text("\n".join(lines), reply_markup=kb_server_details_actions(sid))
            return

        except Exception as e:
            logger.exception("server details error")
            await q.edit_message_text(
                f"⚠️ یک خطای داخلی رخ داد اما ربات زنده است.\n\nعلت: {e}",
                reply_markup=kb_server_details_actions(sid),
            )
            return

    # Delete server
    if q.data.startswith("srv_del:"):
        sid = q.data.split(":", 1)[1]
        if sid in bucket["servers"]:
            del bucket["servers"][sid]
            bucket["order"] = [x for x in bucket["order"] if x != sid]
            save_store(store)
        await q.edit_message_text("✅ سرور حذف شد.", reply_markup=kb_servers(store, user_id))
        return

    # Add server start
    if q.data == "srv_add":
        context.user_data.clear()
        await q.edit_message_text("➕ افزودن سرور جدید\n\nلطفاً IPv4 سرور را ارسال کنید")
        return SRV_ADD_IP

    # Add panel yes/no
    if q.data == "srv_add_panel_yes":
        await q.edit_message_text("خوبه 😌\n\nحالا دامنه پنل را ارسال کنید\nاگر دامنه ندارید /skip بزنید")
        return SRV_ADD_PANEL_DOMAIN

    if q.data == "srv_add_panel_no":
        srv = context.user_data.get("new_server") or {}
        store = load_store()
        bucket = get_user_bucket(store, update.effective_user.id)
        sid = safe_server_id(srv["ip"])
        base = sid
        i = 2
        while sid in bucket["servers"]:
            sid = f"{base}_{i}"
            i += 1
        bucket["servers"][sid] = srv
        bucket["order"].append(sid)
        save_store(store)
        context.user_data.clear()

        text = (
            "✅ سرور شما اضافه شد\n\n"
            f"{fmt_kv('Ipv4:', srv['ip'])}\n"
            f"{fmt_kv('User:', srv['ssh_user'])}\n"
            f"{fmt_kv('Pass:', srv['ssh_pass'])}\n"
            f"{fmt_kv('portssh:', str(srv.get('ssh_port', 22)))}"
        )
        await q.edit_message_text(text, reply_markup=kb_main())
        return ConversationHandler.END

    # scheme pick
    if q.data.startswith("srv_scheme:"):
        scheme = q.data.split(":", 1)[1]
        context.user_data["new_server"]["panel"]["scheme"] = scheme
        await q.edit_message_text("پورت پنل را ارسال کنید (مثلاً 8184)")
        return SRV_ADD_PANEL_PORT

    # Unknown
    await q.edit_message_text("دستور نامعتبر.", reply_markup=kb_main())
    return

# ------------------------- Add server conversation -------------------------
async def srv_add_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = (update.message.text or "").strip()
    if not is_ipv4(ip):
        await update.message.reply_text("این مقدار IPv4 معتبر نیست. لطفاً دوباره ارسال کنید")
        return SRV_ADD_IP
    context.user_data["new_server"] = {"ip": ip}
    await update.message.reply_text("یوزرنیم SSH را ارسال کنید\nاگر root است /skip بزنید")
    return SRV_ADD_SSH_USER

async def srv_add_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "/skip":
        txt = "root"
    context.user_data["new_server"]["ssh_user"] = txt
    await update.message.reply_text("پسورد SSH را ارسال کنید")
    return SRV_ADD_SSH_PASS

async def srv_add_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["ssh_pass"] = (update.message.text or "").strip()
    await update.message.reply_text("پورت SSH را ارسال کنید\nپیش‌فرض 22 است، اگر 22 هست /skip بزنید")
    return SRV_ADD_SSH_PORT

async def srv_add_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "/skip":
        port = 22
    else:
        try:
            port = int(txt)
            if not (1 <= port <= 65535):
                raise ValueError()
        except Exception:
            await update.message.reply_text("پورت معتبر ارسال کنید (1..65535)")
            return SRV_ADD_SSH_PORT

    srv = context.user_data["new_server"]
    srv["ssh_port"] = port

    # ask optional panel
    srv["panel"] = {}
    await update.message.reply_text(
        "می‌خواهید پنل XUI هم برای این سرور ثبت شود؟",
        reply_markup=kb_yes_no_panel(),
    )
    return SRV_ADD_PANEL_ASK

async def srv_add_panel_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "/skip":
        txt = context.user_data["new_server"]["ip"]
    context.user_data["new_server"]["panel"]["domain"] = txt
    await update.message.reply_text("نوع دسترسی پنل را انتخاب کنید", reply_markup=kb_http_https())
    return SRV_ADD_PANEL_SCHEME

async def srv_add_panel_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        port = int((update.message.text or "").strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت معتبر ارسال کنید (1..65535)")
        return SRV_ADD_PANEL_PORT
    context.user_data["new_server"]["panel"]["panel_port"] = port
    await update.message.reply_text("Path پنل را ارسال کنید (مثلاً /tracklessvpn/ یا /)")
    return SRV_ADD_PANEL_PATH

async def srv_add_panel_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = (update.message.text or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    context.user_data["new_server"]["panel"]["panel_path"] = path
    await update.message.reply_text("یوزرنیم پنل را ارسال کنید")
    return SRV_ADD_PANEL_USER

async def srv_add_panel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["panel"]["panel_user"] = (update.message.text or "").strip()
    await update.message.reply_text("پسورد پنل را ارسال کنید")
    return SRV_ADD_PANEL_PASS

async def srv_add_panel_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["panel"]["panel_pass"] = (update.message.text or "").strip()

    srv = context.user_data["new_server"]
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    sid = safe_server_id(srv["ip"])
    base = sid
    i = 2
    while sid in bucket["servers"]:
        sid = f"{base}_{i}"
        i += 1

    bucket["servers"][sid] = srv
    bucket["order"].append(sid)
    save_store(store)
    context.user_data.clear()

    panel = srv.get("panel") or {}
    dom = panel.get("domain") or srv["ip"]
    scheme = panel.get("scheme") or "http"
    pport = str(panel.get("panel_port") or "")
    ppath = panel.get("panel_path") or "/"
    if not ppath.startswith("/"):
        ppath = "/" + ppath
    xui_url = f"{scheme}://{dom}:{pport}{ppath}"

    text = (
        "✅ سرور شما با موفقیت اضافه شد\n\n"
        f"{fmt_kv('Ipv4:', srv['ip'])}\n"
        f"{fmt_kv('User:', srv['ssh_user'])}\n"
        f"{fmt_kv('Pass:', srv['ssh_pass'])}\n"
        f"{fmt_kv('portssh:', str(srv.get('ssh_port', 22)))}\n\n"
        f"{fmt_kv('Xui:', xui_url)}\n"
        f"{fmt_kv('User:', str(panel.get('panel_user') or ''))}\n"
        f"{fmt_kv('Pass:', str(panel.get('panel_pass') or ''))}"
    )
    await update.message.reply_text(text, reply_markup=kb_main())
    return ConversationHandler.END

# ------------------------- Ports Manager (user input only) -------------------------
async def ports_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text("🔀 ثبت لیست پورت‌ها\n\nچند تا پورت دارید؟ (عدد ارسال کنید)")
    return PORTS_COUNT

async def ports_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int((update.message.text or "").strip())
        if not (1 <= n <= 200):
            raise ValueError()
    except Exception:
        await update.message.reply_text("عدد معتبر ارسال کنید (1 تا 200)")
        return PORTS_COUNT

    context.user_data["ports"] = {"count": n, "items": []}
    await update.message.reply_text("پورت پنل را اول ارسال کنید (مثلاً 8184)")
    return PORTS_ITEMS

async def ports_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data.get("ports") or {}
    try:
        p = int((update.message.text or "").strip())
        if not (1 <= p <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت معتبر ارسال کنید (1..65535)")
        return PORTS_ITEMS

    st["items"].append(p)
    idx = len(st["items"])
    if idx < st["count"]:
        await update.message.reply_text(f"پورت بعدی را ارسال کنید ({idx+1}/{st['count']})")
        return PORTS_ITEMS

    # finished
    ports = st["items"]
    context.user_data.clear()
    # تضمین: پورت اول همان پورت پنل است چون اول گرفتیم
    text_lines = ["✅ لیست پورت‌ها ثبت شد", "", "CSV:", f"`{','.join(str(x) for x in ports)}`"]
    await update.message.reply_text("\n".join(text_lines), reply_markup=kb_main())
    return ConversationHandler.END

# ------------------------- Merge flow (ROBUST from your old code) -------------------------
async def merge_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    # user must pick a server first
    rows = []
    for sid in bucket.get("order", []):
        srv = bucket["servers"].get(sid) or {}
        ip = srv.get("ip", sid)
        rows.append([InlineKeyboardButton(ip, callback_data=f"merge_srv:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])

    if not bucket.get("order"):
        await q.edit_message_text("فعلاً هیچ سروری ثبت نشده. اول از مدیریت سرورها سرور اضافه کنید.", reply_markup=kb_main())
        return ConversationHandler.END

    await q.edit_message_text("🔀 ادغام پورت‌ها\n\nاول سرور را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
    return ConversationHandler.END

async def merge_pick_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = q.data.split(":", 1)[1]
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["merge"] = {"sid": sid, "ports": []}

    await q.edit_message_text(
        "🔀 ادغام پورت‌ها\n\n"
        "تعداد پورت‌های ورودی را ارسال کنید (مثلاً 2)"
    )
    return MERGE_COUNT

async def merge_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int((update.message.text or "").strip())
        if not (1 <= n <= 30):
            raise ValueError()
    except Exception:
        await update.message.reply_text("عدد معتبر (1 تا 30) ارسال کنید.")
        return MERGE_COUNT

    context.user_data["merge"]["count"] = n
    context.user_data["merge"]["ports"] = []
    await update.message.reply_text("حالا پورت‌ها را یکی‌یکی ارسال کنید (پورت 1)")
    return MERGE_PORTS

async def merge_ports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    try:
        port = int((update.message.text or "").strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت معتبر ارسال کنید.")
        return MERGE_PORTS

    m["ports"].append(port)
    idx = len(m["ports"])
    if idx < m["count"]:
        await update.message.reply_text(f"پورت {idx} ثبت شد. پورت بعدی ({idx+1})")
        return MERGE_PORTS

    await update.message.reply_text("همه ورودی‌ها ثبت شد. پورت مقصد را ارسال کنید (مثلاً 443)")
    return MERGE_TARGET

async def merge_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    try:
        port = int((update.message.text or "").strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except Exception:
        await update.message.reply_text("پورت مقصد معتبر ارسال کنید.")
        return MERGE_TARGET

    m["target_port"] = port
    await update.message.reply_text(
        f"خلاصه عملیات:\n\n"
        f"ورودی‌ها: {m['ports']}\n"
        f"مقصد: {m['target_port']}\n\n"
        f"برای اجرا OK ارسال کنید"
    )
    return MERGE_CONFIRM

async def merge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip().lower() != "ok":
        await update.message.reply_text("برای ادامه فقط OK ارسال کنید.")
        return MERGE_CONFIRM

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    m = context.user_data.get("merge") or {}
    sid = m.get("sid")
    srv = bucket["servers"].get(sid) if sid else None
    if not srv:
        context.user_data.clear()
        await update.message.reply_text("سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    src_ports = m.get("ports") or []
    target_port = int(m.get("target_port") or 0)

    await update.message.reply_text("⏳ در حال اتصال و انجام ادغام...")

    # 1) Find DB
    try:
        code, out, err = await asyncio.wait_for(
            asyncio.to_thread(
                ssh_exec,
                srv["ip"],
                int(srv.get("ssh_port", 22)),
                srv["ssh_user"],
                srv["ssh_pass"],
                FIND_DB_CMD,
            ),
            timeout=45,
        )
    except asyncio.TimeoutError:
        context.user_data.clear()
        await report_error(update, "Timeout", "سرور دیر پاسخ داد. دوباره تلاش کنید.")
        return ConversationHandler.END

    db_path = out.strip().splitlines()[-1] if out.strip() else ""
    if code != 0 or not db_path or "NOT_FOUND" in db_path:
        context.user_data.clear()
        await update.message.reply_text("❌ دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم", reply_markup=kb_main())
        return ConversationHandler.END

    # helper
    async def get_inbound_id(port: int) -> Optional[int]:
        cmd = inbound_id_by_port_cmd(db_path, port)
        c2, o2, e2 = await asyncio.to_thread(
            ssh_exec,
            srv["ip"],
            int(srv.get("ssh_port", 22)),
            srv["ssh_user"],
            srv["ssh_pass"],
            cmd,
        )
        v = (o2 or "").strip()
        return int(v) if v.isdigit() else None

    # 2) target inbound
    target_id = await get_inbound_id(target_port)
    if not target_id:
        # debug کمک‌کننده
        cdbg, odbg, edbg = await asyncio.to_thread(
            ssh_exec,
            srv["ip"],
            int(srv.get("ssh_port", 22)),
            srv["ssh_user"],
            srv["ssh_pass"],
            debug_inbounds_tail_cmd(db_path),
        )
        context.user_data.clear()
        await update.message.reply_text(
            f"❌ inbound مقصد با پورت {target_port} پیدا نشد. اول داخل پنل بساز.\n\n"
            f"برای بررسی، چند inbound آخر:\n```text\n{short(odbg or edbg, 1400)}\n```",
            reply_markup=kb_main(),
        )
        return ConversationHandler.END

    # 3) sources
    source_ids = []
    missing = []
    for p in src_ports:
        iid = await get_inbound_id(int(p))
        if not iid:
            missing.append(p)
        else:
            source_ids.append(iid)

    if missing:
        context.user_data.clear()
        await update.message.reply_text(f"❌ این پورت‌ها inbound ندارند: {missing}", reply_markup=kb_main())
        return ConversationHandler.END

    src_ids_csv = ",".join(str(x) for x in source_ids)

    # 4) run merge script
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
                srv["ip"],
                int(srv.get("ssh_port", 22)),
                srv["ssh_user"],
                srv["ssh_pass"],
                remote_cmd,
            ),
            timeout=70,
        )
    except asyncio.TimeoutError:
        context.user_data.clear()
        await report_error(update, "Timeout در Merge", "اسکریپت ادغام طول کشید یا گیر کرد.")
        return ConversationHandler.END

    if code3 != 0:
        context.user_data.clear()
        await update.message.reply_text(f"❌ خطا در ادغام:\n```text\n{short(out3 + '\\n' + err3, 3000)}\n```", reply_markup=kb_main())
        return ConversationHandler.END

    # restart
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                ssh_exec,
                srv["ip"],
                int(srv.get("ssh_port", 22)),
                srv["ssh_user"],
                srv["ssh_pass"],
                "sudo x-ui restart || sudo systemctl restart x-ui || true",
            ),
            timeout=35,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("⚠️ ریستارت سرویس طولانی شد، ولی ادغام انجام شده است.")

    context.user_data.clear()
    await update.message.reply_text(f"✅ ادغام انجام شد.\n```text\n{short(out3, 1600)}\n```", reply_markup=kb_main())
    return ConversationHandler.END

# ------------------------- Backup flows (from your old code, adapted to servers) -------------------------
async def backup_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🗂 مدیریت بکاپ\n\n"
        "📤 گرفتن بکاپ: بکاپ کامل دیتابیس پنل را همین لحظه دریافت می‌کنید.\n"
        "📥 وارد کردن بکاپ: دیتابیس را از فایل بکاپ جایگزین می‌کند.\n\n"
        "این عملیات از طریق SSH انجام می‌شود.",
        reply_markup=kb_backup_menu(),
    )
    return BK_MENU

async def bk_export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    if not bucket.get("order"):
        await q.edit_message_text("فعلاً هیچ سروری ثبت نشده.", reply_markup=kb_main())
        return ConversationHandler.END

    rows = []
    for sid in bucket["order"]:
        srv = bucket["servers"].get(sid) or {}
        ip = srv.get("ip", sid)
        rows.append([InlineKeyboardButton(f"📤 {ip}", callback_data=f"bk_export_srv:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])

    await q.edit_message_text("یک سرور را برای بکاپ انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
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

    remote_cmd = f"""
set -e
sudo cp "{db_path}" "{remote_tmp}"
sudo chmod 644 "{remote_tmp}" || true
echo "{remote_tmp}"
"""
    code, out, err = await asyncio.to_thread(
        ssh_exec,
        srv["ip"],
        int(srv.get("ssh_port", 22)),
        srv["ssh_user"],
        srv["ssh_pass"],
        remote_cmd,
    )
    if code != 0:
        await q.edit_message_text(f"❌ خطا:\n{short(out + '\\n' + err, 3000)}", reply_markup=kb_main())
        return ConversationHandler.END

    remote_file = out.strip().splitlines()[-1] if out.strip() else remote_tmp
    local_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="xuihub_backup_", suffix=".db", delete=False) as f:
            local_path = f.name

        def sftp_download():
            c = ssh_client(srv["ip"], int(srv.get("ssh_port", 22)), srv["ssh_user"], srv["ssh_pass"])
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
            srv["ip"],
            int(srv.get("ssh_port", 22)),
            srv["ssh_user"],
            srv["ssh_pass"],
            f"sudo rm -f '{remote_file}' || true",
        )

    panel = srv.get("panel") or {}
    caption_addr = panel.get("domain") or srv.get("ip", sid)
    caption = build_backup_caption(caption_addr, now_utc)
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
    await q.edit_message_text(
        "📥 وارد کردن بکاپ (Restore)\n\nروش بازیابی را انتخاب کنید:",
        reply_markup=kb_backup_import_mode(),
    )
    return BK_IMPORT_CHOOSE_MODE

async def bk_import_existing_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    if not bucket.get("order"):
        await q.edit_message_text("فعلاً هیچ سروری ثبت نشده.", reply_markup=kb_main())
        return ConversationHandler.END

    rows = []
    for sid in bucket["order"]:
        srv = bucket["servers"].get(sid) or {}
        ip = srv.get("ip", sid)
        rows.append([InlineKeyboardButton(f"🔁 {ip}", callback_data=f"bk_import_srv:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])

    await q.edit_message_text("سرور مقصد برای Restore را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
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

    context.user_data["bk_target_server"] = srv
    await q.edit_message_text(
        "فایل بکاپ دیتابیس را ارسال کنید (فایل .db)\n\n"
        "این عملیات دیتابیس فعلی را جایگزین می‌کند."
    )
    return BK_IMPORT_UPLOAD_FILE

async def bk_import_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("فایل را به صورت Document ارسال کنید.")
        return BK_IMPORT_UPLOAD_FILE

    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(prefix="xuihub_restore_", suffix=".db", delete=False) as f:
        local_path = f.name
    await tg_file.download_to_drive(custom_path=local_path)
    context.user_data["bk_local_file"] = local_path

    await update.message.reply_text(
        "⚠️ هشدار مهم\n\n"
        "این عملیات دیتابیس فعلی را کامل جایگزین می‌کند.\n"
        "اگر مطمئن هستید RESTORE ارسال کنید"
    )
    return BK_IMPORT_CONFIRM

async def bk_import_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip().lower() != "restore":
        await update.message.reply_text("برای ادامه فقط RESTORE ارسال کنید.")
        return BK_IMPORT_CONFIRM

    srv = context.user_data.get("bk_target_server")
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

    try:
        def sftp_upload_and_restore():
            c = ssh_client(srv["ip"], int(srv.get("ssh_port", 22)), srv["ssh_user"], srv["ssh_pass"])
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
            raise RuntimeError(short((out + "\n" + err).strip(), 2500))

        await restart_xui(srv)
        await update.message.reply_text(
            "✅ بکاپ با موفقیت بازیابی شد.\n\n"
            f"📌 بکاپ قبلی (برای اطمینان) ذخیره شد:\n{remote_backup_old}"
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

# Import new server (no save) - kept from your old logic
async def bk_import_new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data["new_ssh"] = {}
    await q.edit_message_text("➕ سرور جدید (بدون ذخیره)\n\nSSH Host را ارسال کنید")
    return BK_IMPORT_NEW_SSH_HOST

async def bk_new_ssh_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["host"] = (update.message.text or "").strip()
    await update.message.reply_text("SSH Username را ارسال کنید")
    return BK_IMPORT_NEW_SSH_USER

async def bk_new_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["user"] = (update.message.text or "").strip()
    await update.message.reply_text("SSH Port را ارسال کنید\nاگر 22 هست /skip بزنید")
    return BK_IMPORT_NEW_SSH_PORT

async def bk_new_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == "/skip":
        p = 22
    else:
        try:
            p = int(txt)
            if not (1 <= p <= 65535):
                raise ValueError()
        except Exception:
            await update.message.reply_text("پورت معتبر ارسال کنید (1..65535).")
            return BK_IMPORT_NEW_SSH_PORT
    context.user_data["new_ssh"]["port"] = p
    await update.message.reply_text("SSH Password را ارسال کنید")
    return BK_IMPORT_NEW_SSH_PASS

async def bk_new_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["pass"] = (update.message.text or "").strip()
    await update.message.reply_text("حالا فایل بکاپ دیتابیس .db را ارسال کنید")
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
        "⚠️ هشدار مهم\n\n"
        "این عملیات دیتابیس فعلی سرور را جایگزین می‌کند.\n"
        "برای ادامه RESTORE ارسال کنید"
    )
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

    srv = {
        "ip": ns["host"],
        "ssh_user": ns["user"],
        "ssh_port": ns["port"],
        "ssh_pass": ns["pass"],
        "panel": {"domain": ns["host"]},
    }

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
            c = ssh_client(srv["ip"], int(srv.get("ssh_port", 22)), srv["ssh_user"], srv["ssh_pass"])
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
            raise RuntimeError(short((out + "\n" + err).strip(), 2500))

        await restart_xui(srv)
        await update.message.reply_text(
            "✅ بکاپ با موفقیت بازیابی شد.\n\n"
            f"📌 بکاپ قبلی ذخیره شد:\n{remote_backup_old}\n\n"
            "هیچ اطلاعاتی از این سرور ذخیره نشد و همه اطلاعات موقت پاک شد."
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

# ------------------------- Backup router -------------------------
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
    if q.data.startswith("bk_export_srv:"):
        return await bk_export_pick_server(update, context)
    if q.data.startswith("bk_import_srv:"):
        return await bk_import_pick_server(update, context)
    if q.data == "backup_menu":
        return await backup_menu_entry(update, context)
    return BK_MENU

# ------------------------- Global error handler -------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
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
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Add server conversation
    conv_add_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(nav_callbacks, pattern=r"^srv_add$")],
        states={
            SRV_ADD_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ip)],
            SRV_ADD_SSH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ssh_user)],
            SRV_ADD_SSH_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ssh_pass)],
            SRV_ADD_SSH_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_ssh_port)],
            SRV_ADD_PANEL_ASK: [CallbackQueryHandler(nav_callbacks, pattern=r"^srv_add_panel_(yes|no)$")],
            SRV_ADD_PANEL_DOMAIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_domain)],
            SRV_ADD_PANEL_SCHEME: [CallbackQueryHandler(nav_callbacks, pattern=r"^srv_scheme:(http|https)$")],
            SRV_ADD_PANEL_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_port)],
            SRV_ADD_PANEL_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_path)],
            SRV_ADD_PANEL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_user)],
            SRV_ADD_PANEL_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_add_panel_pass)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_add_server)

    # Ports manager (user input)
    conv_ports = ConversationHandler(
        entry_points=[CallbackQueryHandler(ports_add_entry, pattern=r"^ports_add$")],
        states={
            PORTS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ports_count)],
            PORTS_ITEMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ports_items)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv_ports)

    # Merge conversation: pick server via callback
    conv_merge = ConversationHandler(
        entry_points=[CallbackQueryHandler(nav_callbacks, pattern=r"^start_merge$")],
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

    # Merge pick server (separate handler)
    app.add_handler(CallbackQueryHandler(merge_pick_server, pattern=r"^merge_srv:"))

    # Backup conversation
    conv_backup = ConversationHandler(
        entry_points=[CallbackQueryHandler(backup_menu_entry, pattern=r"^backup_menu$")],
        states={
            BK_MENU: [CallbackQueryHandler(backup_menu_router)],
            BK_EXPORT_PICK_SERVER: [CallbackQueryHandler(backup_menu_router)],
            BK_IMPORT_CHOOSE_MODE: [CallbackQueryHandler(backup_menu_router)],
            BK_IMPORT_PICK_SERVER: [CallbackQueryHandler(backup_menu_router)],
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

    # Global navigation last
    app.add_handler(
        CallbackQueryHandler(
            nav_callbacks,
            pattern=r"^(back_main|servers|ports_menu|backup_menu|profile|srv:.*|srv_del:.*|srv_add|srv_add_panel_yes|srv_add_panel_no|srv_scheme:(http|https)|ports_add)$",
        )
    )

    app.add_error_handler(on_error)
    logger.info("Bot is starting (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
