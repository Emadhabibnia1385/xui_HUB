import os
import json
import re
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List

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

# =========================
# Storage (servers + optional panel)
# =========================
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
    sid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", ip.strip())
    return sid or "server"

# =========================
# Jalali (Shamsi) conversion (used in backup captions)
# =========================
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

# =========================
# SSH helpers
# =========================
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

def list_inbound_ports_cmd(db_path: str) -> str:
    return f"""sudo sqlite3 "{db_path}" "SELECT port FROM inbounds ORDER BY port ASC;" """

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

# =========================
# States
# =========================
(
    # merge
    MERGE_COUNT, MERGE_PORTS, MERGE_TARGET, MERGE_CONFIRM,

    # backup
    BK_MENU, BK_EXPORT_PICK_SERVER, BK_IMPORT_CHOOSE_MODE,
    BK_IMPORT_PICK_SERVER, BK_IMPORT_UPLOAD_FILE, BK_IMPORT_CONFIRM,
    BK_IMPORT_NEW_SSH_HOST, BK_IMPORT_NEW_SSH_USER, BK_IMPORT_NEW_SSH_PORT, BK_IMPORT_NEW_SSH_PASS,
    BK_IMPORT_NEW_UPLOAD_FILE, BK_IMPORT_NEW_CONFIRM,

    # add server + optional panel
    SV_IP, SV_SSH_USER, SV_SSH_PASS, SV_SSH_PORT,
    SV_ASK_ADD_PANEL, SV_PANEL_DOMAIN, SV_PANEL_SCHEME, SV_PANEL_PORT,
    SV_PANEL_PATH, SV_PANEL_USER, SV_PANEL_PASS,

    # edit server (button based)
    EDIT_MENU, EDIT_INPUT,
) = range(29)

# =========================
# Keyboards
# =========================
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥 مدیریت سرورها", callback_data="manage_servers")],
        [InlineKeyboardButton("🔀 مدیریت پورت و کانفیگ", callback_data="start_merge")],
        [InlineKeyboardButton("🗂 مدیریت بکاپ", callback_data="backup_menu")],
        [InlineKeyboardButton("👤 پروفایل", callback_data="profile")],
    ])

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])

def kb_yes_no(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تایید", callback_data=f"{prefix}:yes"),
         InlineKeyboardButton("❌ خیر", callback_data=f"{prefix}:no")],
    ])

def kb_sv_http_https() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 HTTP", callback_data="sv_scheme:http"),
         InlineKeyboardButton("🔐 HTTPS", callback_data="sv_scheme:https")],
    ])

def kb_ed_http_https() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 HTTP", callback_data="ed_scheme:http"),
         InlineKeyboardButton("🔐 HTTPS", callback_data="ed_scheme:https")],
    ])

def kb_servers(store: Dict[str, Any], user_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(store, user_id)
    rows = [[InlineKeyboardButton("➕ افزودن سرور جدید", callback_data="add_server")]]
    for sid in bucket.get("order", []):
        s = bucket["servers"].get(sid, {})
        ip = s.get("ip", sid)
        dom = (s.get("panel") or {}).get("domain")
        title = f"🌐 {ip}" + (f" ({dom})" if dom else "")
        rows.append([InlineKeyboardButton(title, callback_data=f"server:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_server_actions(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ ویرایش اطلاعات", callback_data=f"edit_server:{sid}")],
        [InlineKeyboardButton("🗑 حذف سرور", callback_data=f"del_server:{sid}")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="manage_servers")],
    ])

def kb_backup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 گرفتن بکاپ", callback_data="bk_export")],
        [InlineKeyboardButton("📥 وارد کردن بکاپ", callback_data="bk_import")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")],
    ])

def kb_backup_import_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 از سرورهای ذخیره‌شده", callback_data="bk_import_existing")],
        [InlineKeyboardButton("➕ سرور جدید (بدون ذخیره)", callback_data="bk_import_new")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")],
    ])

def kb_edit_menu(sid: str, has_panel: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📝 ویرایش IPv4", callback_data=f"edit_field:{sid}:ip")],
        [InlineKeyboardButton("🧑‍💻 ویرایش SSH User", callback_data=f"edit_field:{sid}:ssh_user")],
        [InlineKeyboardButton("🔑 ویرایش SSH Pass", callback_data=f"edit_field:{sid}:ssh_pass")],
        [InlineKeyboardButton("🔢 ویرایش SSH Port", callback_data=f"edit_field:{sid}:ssh_port")],
    ]
    if has_panel:
        rows += [
            [InlineKeyboardButton("🌍 ویرایش دامنه پنل", callback_data=f"edit_field:{sid}:panel_domain")],
            [InlineKeyboardButton("🔒 تغییر HTTP/HTTPS", callback_data=f"edit_field:{sid}:panel_scheme")],
            [InlineKeyboardButton("🔢 ویرایش پورت پنل", callback_data=f"edit_field:{sid}:panel_port")],
            [InlineKeyboardButton("🧭 ویرایش Path پنل", callback_data=f"edit_field:{sid}:panel_path")],
            [InlineKeyboardButton("👤 ویرایش User پنل", callback_data=f"edit_field:{sid}:panel_user")],
            [InlineKeyboardButton("🔑 ویرایش Pass پنل", callback_data=f"edit_field:{sid}:panel_pass")],
            [InlineKeyboardButton("🧹 حذف پنل از سرور", callback_data=f"edit_field:{sid}:panel_remove")],
        ]
    else:
        rows += [[InlineKeyboardButton("➕ افزودن پنل برای این سرور", callback_data=f"edit_field:{sid}:panel_add")]]
    rows += [[InlineKeyboardButton("⬅️ بازگشت", callback_data=f"server:{sid}")]]
    return InlineKeyboardMarkup(rows)

# =========================
# Text helpers
# =========================
START_TEXT = (
    "🤖 **به xui_HUB خوش آمدید**\n\n"
    "این ربات برای مدیریت **سرورها** و پنل‌های **3x-ui / x-ui** ساخته شده است.\n\n"
    "✨ امکانات اصلی:\n"
    "• افزودن سرور و ذخیره اطلاعات ✅\n"
    "• افزودن پنل XUI به سرور 🧩\n"
    "• استخراج خودکار پورت‌ها از دیتابیس ⚡️\n"
    "• بکاپ و ریستور دیتابیس 🗂\n\n"
    "از منوی زیر انتخاب کنید 👇\n\n"
    "👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)

def build_server_added_message(server: Dict[str, Any]) -> str:
    ip = server.get("ip","")
    ssh_user = server.get("ssh_user","")
    ssh_pass = server.get("ssh_pass","")
    ssh_port = server.get("ssh_port", 22)
    return (
        "✅ **سرور شما با موفقیت اضافه شد** 🎉\n\n"
        "📌 اطلاعات سرور:\n"
        f"`Ipv4: {ip}`\n"
        f"`User: {ssh_user}`\n"
        f"`Pass: {ssh_pass}`\n"
        f"`portssh:{ssh_port}`\n"
    )

def build_panel_added_message(server: Dict[str, Any]) -> str:
    panel = server.get("panel") or {}
    domain = panel.get("domain") or server.get("ip","")
    scheme = panel.get("scheme","http")
    port = panel.get("port","")
    path = panel.get("path","/")
    url = f"{scheme}://{domain}:{port}{path}"
    return (
        "\n✅ **پنل XUI هم با موفقیت ثبت شد** 🧩\n\n"
        "📌 اطلاعات پنل:\n"
        f"`Xui: {url}`\n"
        f"`User: {panel.get('user','')}`\n"
        f"`Pass: {panel.get('pass','')}`\n"
    )

def build_server_details_text(server: Dict[str, Any], ports: Optional[List[int]]) -> str:
    ip = server.get("ip","")
    ssh_user = server.get("ssh_user","")
    ssh_pass = server.get("ssh_pass","")
    ssh_port = server.get("ssh_port", 22)

    panel = server.get("panel") or {}
    domain = panel.get("domain","")
    scheme = panel.get("scheme","http")
    pport = panel.get("port")
    ppath = panel.get("path","/")
    puser = panel.get("user","")
    ppass = panel.get("pass","")

    parts = []
    parts.append("🖥 **اطلاعات سرور**")
    parts.append(f"`Ipv4: {ip}`")
    parts.append(f"`User: {ssh_user}`")
    parts.append(f"`Pass: {ssh_pass}`")
    parts.append(f"`portssh:{ssh_port}`")
    parts.append("")

    if domain:
        parts.append("🌍 **Paneldomin:**")
        parts.append(f"`{domain}`")
        parts.append("")

    if pport:
        url = f"{scheme}://{domain or ip}:{pport}{ppath}"
        parts.append("🧩 **اطلاعات پنل XUI**")
        parts.append(f"`Xui: {url}`")
        parts.append(f"`User: {puser}`")
        parts.append(f"`Pass: {ppass}`")
        parts.append("")
        parts.append(f"`Port panel: {pport}`")
        parts.append("")

    if ports is None:
        parts.append("⚠️ **Port ها**")
        parts.append("`خطا در دریافت پورت‌ها (ممکن است sudo/sqlite3 در دسترس نباشد یا دیتابیس پیدا نشد)`")
    else:
        parts.append("⚡️ **Port ها:**")
        if ports:
            for x in ports:
                parts.append(f"`{x}`")
            parts.append("")
            parts.append("📌 لیست یک‌خطی:")
            parts.append(f"`{','.join(str(x) for x in ports)}`")
        else:
            parts.append("`هیچ پورتی پیدا نشد.`")

    return "\n".join(parts)

# =========================
# DB access helpers
# =========================
async def find_db_path(server: Dict[str, Any]) -> Optional[str]:
    code, out, err = await asyncio.to_thread(
        ssh_exec,
        server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"],
        FIND_DB_CMD
    )
    db_path = out.strip().splitlines()[-1] if out.strip() else ""
    if "NOT_FOUND" in db_path or not db_path:
        return None
    return db_path

async def restart_xui(server: Dict[str, Any]) -> None:
    await asyncio.to_thread(
        ssh_exec,
        server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"],
        "sudo x-ui restart || sudo systemctl restart x-ui || true"
    )

async def get_inbound_ports(server: Dict[str, Any]) -> Optional[List[int]]:
    db_path = await find_db_path(server)
    if not db_path:
        return None
    code, out, err = await asyncio.to_thread(
        ssh_exec,
        server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"],
        list_inbound_ports_cmd(db_path)
    )
    if code != 0:
        return None
    ports: List[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            ports.append(int(line))
    return ports

# =========================
# Start + Navigation
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")

async def nav_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    if q.data == "back_main":
        await q.edit_message_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")
        return

    if q.data == "manage_servers":
        await q.edit_message_text(
            "🖥 **مدیریت سرورها**\n\n"
            "از این بخش می‌توانید سرورها را اضافه کنید و اطلاعات آن‌ها را مشاهده/ویرایش/حذف کنید.",
            reply_markup=kb_servers(store, user_id),
            parse_mode="Markdown"
        )
        return

    if q.data == "start_merge":
        if not bucket["order"]:
            await q.edit_message_text("ابتدا یک سرور اضافه کنید.", reply_markup=kb_servers(store, user_id))
            return
        rows = []
        for sid in bucket["order"]:
            s = bucket["servers"].get(sid, {})
            ip = s.get("ip", sid)
            rows.append([InlineKeyboardButton(f"🔀 {ip}", callback_data=f"merge:{sid}")])
        rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
        await q.edit_message_text(
            "🔀 **مدیریت پورت و کانفیگ**\n\n"
            "سروری که می‌خواهید عملیات ادغام روی آن انجام شود را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown"
        )
        return

    if q.data == "profile":
        u = update.effective_user
        username = f"@{u.username}" if u.username else "ندارد"
        servers_count = len(bucket.get("order", []))
        server_list = "\n".join([f"• {bucket['servers'][sid].get('ip', sid)}" for sid in bucket.get("order", [])]) if servers_count else "—"
        text = (
            "👤 **پروفایل شما**\n\n"
            f"نام: {u.full_name}\n"
            f"یوزرنیم: {username}\n"
            f"User ID: {u.id}\n\n"
            f"تعداد سرورها: {servers_count}\n"
            f"لیست سرورها:\n{server_list}"
        )
        await q.edit_message_text(text, reply_markup=kb_back_main(), parse_mode="Markdown")
        return

    if q.data.startswith("server:"):
        sid = q.data.split(":", 1)[1]
        s = bucket["servers"].get(sid)
        if not s:
            await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_servers(store, user_id))
            return

        await q.edit_message_text("⏳ در حال استخراج پورت‌ها از سرور...")

        ports = await get_inbound_ports({
            "ssh_host": s["ssh_host"],
            "ssh_port": s["ssh_port"],
            "ssh_user": s["ssh_user"],
            "ssh_pass": s["ssh_pass"],
        })

        text = build_server_details_text(s, ports)
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_server_actions(sid))
        return

    if q.data.startswith("del_server:"):
        sid = q.data.split(":", 1)[1]
        if sid in bucket["servers"]:
            del bucket["servers"][sid]
            bucket["order"] = [x for x in bucket["order"] if x != sid]
            save_store(store)
        await q.edit_message_text("✅ سرور حذف شد.", reply_markup=kb_servers(store, user_id))
        return

    if q.data.startswith("edit_server:"):
        sid = q.data.split(":", 1)[1]
        s = bucket["servers"].get(sid)
        if not s:
            await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_servers(store, user_id))
            return

        context.user_data.clear()
        context.user_data["edit_sid"] = sid

        ports = await get_inbound_ports({
            "ssh_host": s["ssh_host"],
            "ssh_port": s["ssh_port"],
            "ssh_user": s["ssh_user"],
            "ssh_pass": s["ssh_pass"],
        })

        text = (
            "✏️ **ویرایش اطلاعات سرور**\n\n"
            "📌 وضعیت فعلی:\n"
            f"{build_server_details_text(s, ports)}\n\n"
            "حالا یکی از گزینه‌های ویرایش را انتخاب کنید 👇"
        )
        await q.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=kb_edit_menu(sid, has_panel=bool(s.get("panel")))
        )
        return

# =========================
# Add Server Flow (with optional panel)
# =========================
async def add_server_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data.clear()
    context.user_data["new_server"] = {}

    await q.edit_message_text(
        "➕ **افزودن سرور جدید** 🖥\n\n"
        "لطفاً **IPv4** سرور را ارسال کنید:\n"
        "مثال:\n"
        "`159.65.243.137`",
        parse_mode="Markdown"
    )
    return SV_IP

async def sv_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = update.message.text.strip()
    context.user_data["new_server"]["ip"] = ip

    await update.message.reply_text(
        "👤 **یوزرنیم SSH** را ارسال کنید.\n\n"
        "اگر یوزرنیم شما **root** است، دستور زیر را بزنید:\n"
        "`/skip`",
        parse_mode="Markdown"
    )
    return SV_SSH_USER

async def sv_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    context.user_data["new_server"]["ssh_user"] = "root" if txt == "/skip" else txt

    await update.message.reply_text(
        "🔑 **پسورد SSH** را ارسال کنید:",
        parse_mode="Markdown"
    )
    return SV_SSH_PASS

async def sv_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["ssh_pass"] = update.message.text.strip()

    await update.message.reply_text(
        "🔢 **پورت SSH** را ارسال کنید.\n\n"
        "اگر پورت شما **22** است، دستور زیر را بزنید:\n"
        "`/skip`",
        parse_mode="Markdown"
    )
    return SV_SSH_PORT

async def sv_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt == "/skip":
        ssh_port = 22
    else:
        try:
            ssh_port = int(txt)
            if not (1 <= ssh_port <= 65535):
                raise ValueError()
        except:
            await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535) یا `/skip`.", parse_mode="Markdown")
            return SV_SSH_PORT

    context.user_data["new_server"]["ssh_port"] = ssh_port

    await update.message.reply_text(
        "🧩 آیا می‌خواهید برای این سرور **پنل XUI** هم اضافه کنید؟",
        reply_markup=kb_yes_no("sv_add_panel"),
        parse_mode="Markdown"
    )
    return SV_ASK_ADD_PANEL

async def sv_ask_add_panel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":", 1)[1]

    if choice == "no":
        store = load_store()
        bucket = get_user_bucket(store, update.effective_user.id)

        s = context.user_data.get("new_server", {})
        ip = s.get("ip","")
        sid = safe_server_id(ip)
        base = sid
        i = 2
        while sid in bucket["servers"]:
            sid = f"{base}_{i}"
            i += 1

        server_obj = {
            "ip": s["ip"],
            "ssh_host": s["ip"],
            "ssh_user": s["ssh_user"],
            "ssh_pass": s["ssh_pass"],
            "ssh_port": s["ssh_port"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "panel": None,
        }
        bucket["servers"][sid] = server_obj
        bucket["order"].append(sid)
        save_store(store)
        context.user_data.clear()

        await q.edit_message_text(
            build_server_added_message(server_obj) + "\n\n"
            "از منوی اصلی می‌توانید ادامه بدهید 👇",
            parse_mode="Markdown",
            reply_markup=kb_main()
        )
        return ConversationHandler.END

    await q.edit_message_text(
        "🌍 **دامنه پنل** را ارسال کنید.\n\n"
        "اگر دامنه ندارید، دستور زیر را بزنید تا همان IP استفاده شود:\n"
        "`/skip`",
        parse_mode="Markdown"
    )
    return SV_PANEL_DOMAIN

async def sv_panel_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    ip = context.user_data["new_server"]["ip"]
    domain = ip if txt == "/skip" else txt
    context.user_data["new_server"]["panel"] = {"domain": domain}

    await update.message.reply_text(
        "🔒 **نوع دسترسی پنل** را انتخاب کنید:",
        reply_markup=kb_sv_http_https()
    )
    return SV_PANEL_SCHEME

async def sv_panel_scheme_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    scheme = q.data.split(":", 1)[1]
    context.user_data["new_server"]["panel"]["scheme"] = scheme

    await q.edit_message_text(
        "🔢 **پورت پنل** را ارسال کنید:\n"
        "مثال:\n"
        "`8184`",
        parse_mode="Markdown"
    )
    return SV_PANEL_PORT

async def sv_panel_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        p = int(update.message.text.strip())
        if not (1 <= p <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
        return SV_PANEL_PORT

    context.user_data["new_server"]["panel"]["port"] = p
    await update.message.reply_text(
        "🧭 **Path پنل** را ارسال کنید.\n\n"
        "مثال:\n"
        "`/tracklessvpn/`\n"
        "یا اگر ندارید:\n"
        "`/`",
        parse_mode="Markdown"
    )
    return SV_PANEL_PATH

async def sv_panel_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = update.message.text.strip()
    if not path.startswith("/"):
        path = "/" + path
    context.user_data["new_server"]["panel"]["path"] = path

    await update.message.reply_text("👤 **یوزرنیم پنل** را ارسال کنید:", parse_mode="Markdown")
    return SV_PANEL_USER

async def sv_panel_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["panel"]["user"] = update.message.text.strip()
    await update.message.reply_text("🔑 **پسورد پنل** را ارسال کنید:", parse_mode="Markdown")
    return SV_PANEL_PASS

async def sv_panel_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_server"]["panel"]["pass"] = update.message.text.strip()

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    s = context.user_data["new_server"]
    ip = s.get("ip","")
    sid = safe_server_id(ip)
    base = sid
    i = 2
    while sid in bucket["servers"]:
        sid = f"{base}_{i}"
        i += 1

    server_obj = {
        "ip": s["ip"],
        "ssh_host": s["ip"],
        "ssh_user": s["ssh_user"],
        "ssh_pass": s["ssh_pass"],
        "ssh_port": s["ssh_port"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "panel": s["panel"],
    }
    bucket["servers"][sid] = server_obj
    bucket["order"].append(sid)
    save_store(store)

    context.user_data.clear()

    text = build_server_added_message(server_obj) + build_panel_added_message(server_obj) + "\n\n"
    text += "از منوی اصلی می‌توانید ادامه بدهید 👇"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_main())
    return ConversationHandler.END

# =========================
# Edit Server Flow (button based)
# =========================
async def edit_router_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    _, sid, field = q.data.split(":", 2)
    s = bucket["servers"].get(sid)
    if not s:
        await q.edit_message_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    # actions without input
    if field == "panel_remove":
        s["panel"] = None
        save_store(store)

        ports = await get_inbound_ports({
            "ssh_host": s["ssh_host"],
            "ssh_port": s["ssh_port"],
            "ssh_user": s["ssh_user"],
            "ssh_pass": s["ssh_pass"],
        })

        await q.edit_message_text(
            "✅ پنل از سرور حذف شد.\n\n"
            f"{build_server_details_text(s, ports)}\n\n"
            "گزینه بعدی را انتخاب کنید 👇",
            parse_mode="Markdown",
            reply_markup=kb_edit_menu(sid, has_panel=False)
        )
        return EDIT_MENU

    if field == "panel_add":
        # ask for domain first (message input)
        context.user_data["edit_sid"] = sid
        context.user_data["edit_field"] = "panel_domain"
        await q.edit_message_text(
            "➕ **افزودن پنل برای این سرور** 🧩\n\n"
            "🌍 دامنه پنل را ارسال کنید.\n"
            "اگر دامنه ندارید، `/skip` بزنید تا همان IP استفاده شود.",
            parse_mode="Markdown"
        )
        return EDIT_INPUT

    if field == "panel_scheme":
        context.user_data["edit_sid"] = sid
        context.user_data["edit_field"] = "panel_scheme"
        await q.edit_message_text("🔒 نوع دسترسی پنل را انتخاب کنید:", reply_markup=kb_ed_http_https())
        return EDIT_MENU

    # input-required fields
    context.user_data["edit_sid"] = sid
    context.user_data["edit_field"] = field

    prompts = {
        "ip": "📝 **ویرایش IPv4**\n\nلطفاً IP جدید را ارسال کنید:\n`159.65.243.137`",
        "ssh_user": "🧑‍💻 **ویرایش SSH User**\n\nیوزرنیم جدید را ارسال کنید.\nاگر root است: `/skip`",
        "ssh_pass": "🔑 **ویرایش SSH Pass**\n\nپسورد جدید را ارسال کنید:",
        "ssh_port": "🔢 **ویرایش SSH Port**\n\nپورت جدید را ارسال کنید.\nاگر 22 است: `/skip`",
        "panel_domain": "🌍 **ویرایش دامنه پنل**\n\nدامنه جدید را ارسال کنید.\nاگر ندارید: `/skip` (یعنی همان IP)",
        "panel_port": "🔢 **ویرایش پورت پنل**\n\nپورت جدید را ارسال کنید:",
        "panel_path": "🧭 **ویرایش Path پنل**\n\nPath جدید را ارسال کنید (مثلاً `/tracklessvpn/` یا `/`):",
        "panel_user": "👤 **ویرایش User پنل**\n\nیوزرنیم جدید پنل را ارسال کنید:",
        "panel_pass": "🔑 **ویرایش Pass پنل**\n\nپسورد جدید پنل را ارسال کنید:",
    }
    await q.edit_message_text(prompts.get(field, "لطفاً مقدار جدید را ارسال کنید:"), parse_mode="Markdown")
    return EDIT_INPUT

async def edit_input_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    sid = context.user_data.get("edit_sid")
    field = context.user_data.get("edit_field")

    if not sid or not field:
        await update.message.reply_text("❌ جلسه ویرایش معتبر نیست.", reply_markup=kb_main())
        return ConversationHandler.END

    s = bucket["servers"].get(sid)
    if not s:
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    txt = update.message.text.strip()

    def ensure_panel():
        if not s.get("panel"):
            s["panel"] = {
                "domain": s.get("ip",""),
                "scheme": "http",
                "port": 0,
                "path": "/",
                "user": "",
                "pass": "",
            }

    try:
        if field == "ip":
            s["ip"] = txt
            s["ssh_host"] = txt
        elif field == "ssh_user":
            s["ssh_user"] = "root" if txt == "/skip" else txt
        elif field == "ssh_pass":
            s["ssh_pass"] = txt
        elif field == "ssh_port":
            s["ssh_port"] = 22 if txt == "/skip" else int(txt)
            if not (1 <= int(s["ssh_port"]) <= 65535):
                raise ValueError()
        elif field == "panel_domain":
            ensure_panel()
            s["panel"]["domain"] = s.get("ip","") if txt == "/skip" else txt
        elif field == "panel_port":
            ensure_panel()
            p = int(txt)
            if not (1 <= p <= 65535):
                raise ValueError()
            s["panel"]["port"] = p
        elif field == "panel_path":
            ensure_panel()
            path = txt
            if not path.startswith("/"):
                path = "/" + path
            s["panel"]["path"] = path
        elif field == "panel_user":
            ensure_panel()
            s["panel"]["user"] = txt
        elif field == "panel_pass":
            ensure_panel()
            s["panel"]["pass"] = txt
        else:
            await update.message.reply_text("❌ فیلد ناشناخته است.", reply_markup=kb_main())
            return ConversationHandler.END

        save_store(store)

        ports = await get_inbound_ports({
            "ssh_host": s["ssh_host"],
            "ssh_port": s["ssh_port"],
            "ssh_user": s["ssh_user"],
            "ssh_pass": s["ssh_pass"],
        })

        await update.message.reply_text(
            "✅ **ویرایش با موفقیت انجام شد**\n\n"
            f"{build_server_details_text(s, ports)}\n\n"
            "اگر باز هم نیاز به ویرایش دارید، از منوی زیر انتخاب کنید 👇",
            parse_mode="Markdown",
            reply_markup=kb_edit_menu(sid, has_panel=bool(s.get("panel")))
        )
        return EDIT_MENU

    except:
        await update.message.reply_text("❌ مقدار معتبر نیست. دوباره ارسال کنید.")
        return EDIT_INPUT

async def edit_scheme_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    scheme = q.data.split(":", 1)[1]

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    sid = context.user_data.get("edit_sid")
    if not sid:
        await q.edit_message_text("❌ جلسه ویرایش معتبر نیست.", reply_markup=kb_main())
        return ConversationHandler.END

    s = bucket["servers"].get(sid)
    if not s:
        await q.edit_message_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    if not s.get("panel"):
        s["panel"] = {"domain": s.get("ip",""), "scheme": "http", "port": 0, "path": "/", "user": "", "pass": ""}

    s["panel"]["scheme"] = scheme
    save_store(store)

    ports = await get_inbound_ports({
        "ssh_host": s["ssh_host"],
        "ssh_port": s["ssh_port"],
        "ssh_user": s["ssh_user"],
        "ssh_pass": s["ssh_pass"],
    })

    await q.edit_message_text(
        "✅ نوع دسترسی پنل تغییر کرد.\n\n"
        f"{build_server_details_text(s, ports)}\n\n"
        "حالا می‌توانید سایر گزینه‌ها را هم ویرایش کنید 👇",
        parse_mode="Markdown",
        reply_markup=kb_edit_menu(sid, has_panel=True)
    )
    return EDIT_MENU

# =========================
# Merge flow (as-is, now uses server)
# =========================
async def merge_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    sid = q.data.split(":", 1)[1]
    if sid not in bucket["servers"]:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_servers(store, update.effective_user.id))
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["merge"] = {"server_id": sid, "ports": []}

    await q.edit_message_text(
        "🔀 **ادغام پورت‌ها**\n\n"
        "⚠️ پورت مقصد را **از قبل** داخل پنل ساخته باشید.\n\n"
        "تعداد پورت‌های ورودی را ارسال کنید (مثلاً 2):",
        parse_mode="Markdown"
    )
    return MERGE_COUNT

async def merge_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if not (1 <= n <= 30):
            raise ValueError()
    except:
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
    except:
        await update.message.reply_text("پورت معتبر ارسال کنید.")
        return MERGE_PORTS

    m["ports"].append(port)
    idx = len(m["ports"])
    if idx < m["count"]:
        await update.message.reply_text(f"✅ پورت {idx} ثبت شد. پورت بعدی (پورت {idx+1}):")
        return MERGE_PORTS

    await update.message.reply_text("✅ همه ورودی‌ها ثبت شد. پورت مقصد را ارسال کنید (مثلاً 443):")
    return MERGE_TARGET

async def merge_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("پورت مقصد معتبر ارسال کنید.")
        return MERGE_TARGET

    m["target_port"] = port
    await update.message.reply_text(
        "🧾 **خلاصه عملیات**\n\n"
        f"ورودی‌ها: `{m['ports']}`\n"
        f"مقصد: `{m['target_port']}`\n\n"
        "برای اجرا `OK` ارسال کنید:",
        parse_mode="Markdown"
    )
    return MERGE_CONFIRM

async def merge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "ok":
        await update.message.reply_text("برای ادامه فقط `OK` ارسال کنید.", parse_mode="Markdown")
        return MERGE_CONFIRM

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    sid = context.user_data["merge"]["server_id"]
    server = bucket["servers"].get(sid)
    if not server:
        context.user_data.clear()
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    src_ports = context.user_data["merge"]["ports"]
    target_port = context.user_data["merge"]["target_port"]

    await update.message.reply_text("⏳ در حال اتصال و انجام ادغام...")

    code, out, err = await asyncio.to_thread(
        ssh_exec,
        server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"],
        FIND_DB_CMD
    )
    db_path = out.strip().splitlines()[-1] if out.strip() else ""
    if "NOT_FOUND" in db_path or not db_path:
        context.user_data.clear()
        await update.message.reply_text("❌ دیتابیس x-ui.db پیدا نشد یا sudo ندارم.", reply_markup=kb_main())
        return ConversationHandler.END

    def get_inbound_id(port: int) -> Optional[int]:
        c, o, e = ssh_exec(server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"],
                          inbound_id_by_port_cmd(db_path, port))
        v = o.strip()
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

    code, out, err = await asyncio.to_thread(
        ssh_exec,
        server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"],
        remote_cmd
    )
    if code != 0:
        context.user_data.clear()
        msg = (out + "\n" + err).strip()
        await update.message.reply_text(f"❌ خطا:\n{msg[:3500]}", reply_markup=kb_main())
        return ConversationHandler.END

    await restart_xui({
        "ssh_host": server["ssh_host"],
        "ssh_port": server["ssh_port"],
        "ssh_user": server["ssh_user"],
        "ssh_pass": server["ssh_pass"],
    })

    context.user_data.clear()
    await update.message.reply_text(f"✅ ادغام انجام شد.\n{out.strip()}", reply_markup=kb_main())
    return ConversationHandler.END

# =========================
# Backup flow (kept as before; just uses servers list)
# =========================
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

async def backup_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🗂 **مدیریت بکاپ**\n\n"
        "• 📤 گرفتن بکاپ: بکاپ کامل پنل را همین لحظه دریافت می‌کنید.\n"
        "• 📥 وارد کردن بکاپ: بازیابی دیتابیس از فایل بکاپ.\n\n"
        "⚠️ این عملیات از طریق SSH انجام می‌شود.",
        reply_markup=kb_backup_menu(),
        parse_mode="Markdown"
    )
    return BK_MENU

async def bk_export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    if not bucket["order"]:
        await q.edit_message_text("ابتدا یک سرور اضافه کنید.", reply_markup=kb_servers(store, update.effective_user.id))
        return ConversationHandler.END

    rows = []
    for sid in bucket["order"]:
        s = bucket["servers"].get(sid, {})
        ip = s.get("ip", sid)
        rows.append([InlineKeyboardButton(f"📤 {ip}", callback_data=f"bk_export_server:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])

    await q.edit_message_text("📤 سرور موردنظر برای بکاپ را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
    return BK_EXPORT_PICK_SERVER

async def bk_export_pick_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    sid = q.data.split(":", 1)[1]
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    server = bucket["servers"].get(sid)
    if not server:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    await q.edit_message_text("⏳ در حال گرفتن بکاپ...")

    db_path = await find_db_path({
        "ssh_host": server["ssh_host"],
        "ssh_port": server["ssh_port"],
        "ssh_user": server["ssh_user"],
        "ssh_pass": server["ssh_pass"],
    })
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
        server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"],
        remote_cmd
    )
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
            c = ssh_client(server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"])
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
            server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"],
            f"sudo rm -f '{remote_file}' || true"
        )

    caption = build_backup_caption(server.get("ip", sid), now_utc)
    filename = f"xui_backup_{server.get('ip', sid)}_{ts}.db".replace("/", "_").replace(":", "_")

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
        "📥 **وارد کردن بکاپ (Restore)**\n\nروش بازیابی را انتخاب کنید:",
        reply_markup=kb_backup_import_mode(),
        parse_mode="Markdown"
    )
    return BK_IMPORT_CHOOSE_MODE

async def bk_import_existing_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    if not bucket["order"]:
        await q.edit_message_text("ابتدا یک سرور اضافه کنید.", reply_markup=kb_servers(store, update.effective_user.id))
        return ConversationHandler.END

    context.user_data.clear()
    rows = []
    for sid in bucket["order"]:
        s = bucket["servers"].get(sid, {})
        ip = s.get("ip", sid)
        rows.append([InlineKeyboardButton(f"🔁 {ip}", callback_data=f"bk_import_server:{sid}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="backup_menu")])

    await q.edit_message_text("🔁 سرور مقصد برای Restore را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
    return BK_IMPORT_PICK_SERVER

async def bk_import_pick_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    sid = q.data.split(":", 1)[1]
    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)

    server = bucket["servers"].get(sid)
    if not server:
        await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    context.user_data["bk_target_server"] = server
    await q.edit_message_text(
        "📎 لطفاً **فایل بکاپ دیتابیس** را ارسال کنید (فایل `.db`).\n\n"
        "⚠️ این عملیات دیتابیس فعلی را جایگزین می‌کند.",
        parse_mode="Markdown"
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
        "این عملیات دیتابیس فعلی را به‌طور کامل جایگزین می‌کند.\n"
        "اگر مطمئن هستید، عبارت زیر را ارسال کنید:\n"
        "`RESTORE`",
        parse_mode="Markdown"
    )
    return BK_IMPORT_CONFIRM

async def bk_import_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "restore":
        await update.message.reply_text("برای ادامه فقط `RESTORE` ارسال کنید.", parse_mode="Markdown")
        return BK_IMPORT_CONFIRM

    server = context.user_data.get("bk_target_server")
    local_file = context.user_data.get("bk_local_file")
    if not server or not local_file or not os.path.exists(local_file):
        context.user_data.clear()
        await update.message.reply_text("❌ فایل یا اطلاعات سرور موجود نیست.", reply_markup=kb_main())
        return ConversationHandler.END

    await update.message.reply_text("⏳ در حال Restore بکاپ...")

    db_path = await find_db_path({
        "ssh_host": server["ssh_host"],
        "ssh_port": server["ssh_port"],
        "ssh_user": server["ssh_user"],
        "ssh_pass": server["ssh_pass"],
    })
    if not db_path:
        try: os.remove(local_file)
        except: pass
        context.user_data.clear()
        await update.message.reply_text("❌ دیتابیس پیدا نشد یا sudo ندارم.", reply_markup=kb_main())
        return ConversationHandler.END

    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%d_%H%M")
    remote_upload = f"/tmp/xuihub_restore_upload_{ts}.db"
    remote_backup_old = f"/tmp/xuihub_old_before_restore_{ts}.db"

    try:
        def sftp_upload_and_restore():
            c = ssh_client(server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"])
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

        await restart_xui({
            "ssh_host": server["ssh_host"],
            "ssh_port": server["ssh_port"],
            "ssh_user": server["ssh_user"],
            "ssh_pass": server["ssh_pass"],
        })

        await update.message.reply_text(
            "✅ بکاپ با موفقیت بازیابی شد.\n\n"
            f"📌 بکاپ قبلی (برای اطمینان) ذخیره شد:\n`{remote_backup_old}`",
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

# new server restore (no save)
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
    except:
        await update.message.reply_text("پورت معتبر ارسال کنید (1..65535).")
        return BK_IMPORT_NEW_SSH_PORT
    context.user_data["new_ssh"]["port"] = p
    await update.message.reply_text("🔑 SSH Password را ارسال کنید:")
    return BK_IMPORT_NEW_SSH_PASS

async def bk_new_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_ssh"]["pass"] = update.message.text.strip()
    await update.message.reply_text("📎 حالا فایل بکاپ دیتابیس `.db` را ارسال کنید:")
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
        "برای ادامه `RESTORE` ارسال کنید:",
        parse_mode="Markdown"
    )
    return BK_IMPORT_NEW_CONFIRM

async def bk_new_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "restore":
        await update.message.reply_text("برای ادامه فقط `RESTORE` ارسال کنید.", parse_mode="Markdown")
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

    server = {"ssh_host": ns["host"], "ssh_user": ns["user"], "ssh_port": ns["port"], "ssh_pass": ns["pass"]}

    await update.message.reply_text("⏳ در حال Restore روی سرور جدید...")

    db_path = await find_db_path(server)
    if not db_path:
        try: os.remove(local_file)
        except: pass
        context.user_data.clear()
        await update.message.reply_text("❌ دیتابیس پیدا نشد یا sudo ندارم.", reply_markup=kb_main())
        return ConversationHandler.END

    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y%m%d_%H%M")
    remote_upload = f"/tmp/xuihub_restore_upload_{ts}.db"
    remote_backup_old = f"/tmp/xuihub_old_before_restore_{ts}.db"

    try:
        def sftp_upload_and_restore_new():
            c = ssh_client(server["ssh_host"], server["ssh_port"], server["ssh_user"], server["ssh_pass"])
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

        code, out, err = await asyncio.to_thread(sftp_upload_and_restore_new)
        if code != 0:
            raise RuntimeError((out + "\n" + err).strip()[:3500])

        await restart_xui(server)

        await update.message.reply_text(
            "✅ بکاپ با موفقیت بازیابی شد.\n\n"
            f"📌 بکاپ قبلی ذخیره شد:\n`{remote_backup_old}`\n\n"
            "ℹ️ هیچ اطلاعاتی از این سرور ذخیره نشد و همه اطلاعات موقت پاک شد.",
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
    if q.data.startswith("bk_export_server:"):
        return await bk_export_pick_server(update, context)
    if q.data.startswith("bk_import_server:"):
        return await bk_import_pick_server(update, context)
    if q.data == "backup_menu":
        return await backup_menu_entry(update, context)
    return BK_MENU

# =========================
# Main
# =========================
def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v

def main():
    token = env_required("TOKEN")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))

    # --- Add server conversation
    conv_add_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_server_entry, pattern="^add_server$")],
        states={
            SV_IP: [MessageHandler(filters.TEXT, sv_ip)],
            SV_SSH_USER: [MessageHandler(filters.TEXT, sv_ssh_user)],
            SV_SSH_PASS: [MessageHandler(filters.TEXT, sv_ssh_pass)],
            SV_SSH_PORT: [MessageHandler(filters.TEXT, sv_ssh_port)],
            SV_ASK_ADD_PANEL: [CallbackQueryHandler(sv_ask_add_panel_cb, pattern=r"^sv_add_panel:(yes|no)$")],
            SV_PANEL_DOMAIN: [MessageHandler(filters.TEXT, sv_panel_domain)],
            SV_PANEL_SCHEME: [CallbackQueryHandler(sv_panel_scheme_cb, pattern=r"^sv_scheme:(http|https)$")],
            SV_PANEL_PORT: [MessageHandler(filters.TEXT, sv_panel_port)],
            SV_PANEL_PATH: [MessageHandler(filters.TEXT, sv_panel_path)],
            SV_PANEL_USER: [MessageHandler(filters.TEXT, sv_panel_user)],
            SV_PANEL_PASS: [MessageHandler(filters.TEXT, sv_panel_pass)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv_add_server)

    # --- Edit server conversation (button based)
    conv_edit_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_router_cb, pattern=r"^edit_field:")],
        states={
            EDIT_MENU: [
                CallbackQueryHandler(edit_router_cb, pattern=r"^edit_field:"),
                CallbackQueryHandler(edit_scheme_cb, pattern=r"^ed_scheme:(http|https)$"),
            ],
            EDIT_INPUT: [MessageHandler(filters.TEXT, edit_input_msg)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv_edit_server)

    # --- Merge conversation
    conv_merge = ConversationHandler(
        entry_points=[CallbackQueryHandler(merge_entry, pattern=r"^merge:")],
        states={
            MERGE_COUNT: [MessageHandler(filters.TEXT, merge_count)],
            MERGE_PORTS: [MessageHandler(filters.TEXT, merge_ports)],
            MERGE_TARGET: [MessageHandler(filters.TEXT, merge_target)],
            MERGE_CONFIRM: [MessageHandler(filters.TEXT, merge_confirm)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv_merge)

    # --- Backup conversation
    conv_backup = ConversationHandler(
        entry_points=[CallbackQueryHandler(backup_menu_entry, pattern="^backup_menu$")],
        states={
            BK_MENU: [CallbackQueryHandler(backup_menu_router)],
            BK_EXPORT_PICK_SERVER: [CallbackQueryHandler(backup_menu_router)],
            BK_IMPORT_CHOOSE_MODE: [CallbackQueryHandler(backup_menu_router)],
            BK_IMPORT_PICK_SERVER: [CallbackQueryHandler(backup_menu_router)],
            BK_IMPORT_UPLOAD_FILE: [MessageHandler(filters.Document.ALL, bk_import_receive_file)],
            BK_IMPORT_CONFIRM: [MessageHandler(filters.TEXT, bk_import_confirm)],
            BK_IMPORT_NEW_SSH_HOST: [MessageHandler(filters.TEXT, bk_new_ssh_host)],
            BK_IMPORT_NEW_SSH_USER: [MessageHandler(filters.TEXT, bk_new_ssh_user)],
            BK_IMPORT_NEW_SSH_PORT: [MessageHandler(filters.TEXT, bk_new_ssh_port)],
            BK_IMPORT_NEW_SSH_PASS: [MessageHandler(filters.TEXT, bk_new_ssh_pass)],
            BK_IMPORT_NEW_UPLOAD_FILE: [MessageHandler(filters.Document.ALL, bk_new_receive_file)],
            BK_IMPORT_NEW_CONFIRM: [MessageHandler(filters.TEXT, bk_new_confirm)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv_backup)

    # Navigation AFTER conversations
    app.add_handler(CallbackQueryHandler(nav_callbacks))

    app.run_polling()

if __name__ == "__main__":
    main()
