import os
import re
import asyncio
import logging
from typing import List, Tuple

import paramiko
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("xuihub_merge_simple")

# ------------------------- .env loader -------------------------
def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        logger.exception("Failed to load .env")

def get_token() -> str:
    token = os.getenv("TOKEN", "").strip()
    if token:
        return token
    load_env_file("/opt/xui_HUB/.env")
    token = os.getenv("TOKEN", "").strip()
    if not token:
        raise RuntimeError("TOKEN not found in env or /opt/xui_HUB/.env")
    return token


START_TEXT = (
    "🤖 به **xuiHUB** خوش آمدید\n\n"
    "🔀 این ربات کلاینت‌های چند Inbound را داخل یک Inbound مقصد ادغام می‌کند.\n"
    "✅ فقط اطلاعات سرور + شماره Inboundها را می‌گیرد و Merge می‌زند.\n"
    "⛔️ هیچ Stop انجام نمی‌دهد.\n\n"
    "برای شروع روی دکمه زیر بزن 👇\n"
    "👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)

def kb_main():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔀 شروع ادغام اینباند", callback_data="start_merge")]])

def kb_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ انجام بده", callback_data="do_merge"),
         InlineKeyboardButton("❌ لغو", callback_data="cancel")]
    ])

def is_ipv4(ip: str) -> bool:
    ip = (ip or "").strip()
    if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", ip):
        return False
    try:
        return all(0 <= int(x) <= 255 for x in ip.split("."))
    except Exception:
        return False

def parse_int(s: str, mn: int, mx: int):
    s = (s or "").strip()
    if not re.fullmatch(r"\d+", s):
        return None
    v = int(s)
    if not (mn <= v <= mx):
        return None
    return v

def _short(s: str, n: int = 3000) -> str:
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")

# ------------------------- SSH helpers -------------------------
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

def ssh_exec_raw(c: paramiko.SSHClient, cmd: str, read_timeout: int = 60) -> Tuple[int, str, str]:
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

def ssh_exec(host: str, port: int, user: str, password: str, cmd: str,
             conn_timeout: int = 20, read_timeout: int = 60) -> Tuple[int, str, str]:
    c = ssh_client(host, port, user, password, timeout=conn_timeout)
    try:
        return ssh_exec_raw(c, cmd, read_timeout=read_timeout)
    finally:
        c.close()

# ------------------------- Build commands depending on root/non-root -------------------------
def sudo_prefix(ssh_user: str) -> str:
    # اگر root هستی، sudo حذف (حل قطعی مشکل تو)
    return "" if (ssh_user or "").strip() == "root" else "sudo -n"

def find_db_cmd(ssh_user: str) -> str:
    S = sudo_prefix(ssh_user)
    # اگر root هست، sudo لازم نیست. اگر نبود، sudo -n استفاده می‌شه.
    return rf"""
set -e
for p in /etc/x-ui/x-ui.db /usr/local/x-ui/x-ui.db /opt/x-ui/x-ui.db /var/lib/x-ui/x-ui.db /root/x-ui.db; do
  if [ -f "$p" ]; then echo "$p"; exit 0; fi
done

if command -v timeout >/dev/null 2>&1; then
  DB=$(timeout 12s {S} find / -maxdepth 6 -name "x-ui.db" 2>/dev/null | head -n 1 || true)
else
  DB=$({S} find / -maxdepth 6 -name "x-ui.db" 2>/dev/null | head -n 1 || true)
fi

if [ -z "$DB" ]; then
  echo "NOT_FOUND"
else
  echo "$DB"
fi
"""

def make_merge_script(ssh_user: str) -> str:
    S = sudo_prefix(ssh_user)
    # sqlite3 path auto-detect
    # مهم: اگر root هستیم، sudo هیچوقت استفاده نمی‌شود.
    return rf"""
set -e
DB="$1"
TARGET_ID="$2"
SRC_IDS="$3"

# پیدا کردن sqlite3
SQLITE_BIN="$(command -v sqlite3 || true)"
if [ -z "$SQLITE_BIN" ]; then
  for p in /usr/bin/sqlite3 /bin/sqlite3 /usr/local/bin/sqlite3; do
    if [ -f "$p" ]; then SQLITE_BIN="$p"; break; fi
  done
fi

if [ -z "$SQLITE_BIN" ] || [ ! -f "$SQLITE_BIN" ]; then
  echo "ERR_NO_SQLITE3"
  exit 10
fi

# اگر sudo استفاده می‌شود، چک کن بدون پسورد است
if [ -n "{S}" ]; then
  if ! sudo -n true >/dev/null 2>&1; then
    echo "ERR_SUDO_NEEDS_PASSWORD"
    exit 40
  fi
fi

command -v python3 >/dev/null 2>&1 || {{ echo "ERR_NO_PYTHON3"; exit 13; }}

{S} cp "$DB" "/tmp/xuihub_db_backup_$(date +%s).db" >/dev/null 2>&1 || true

HAS_CLIENTS=$({S} "$SQLITE_BIN" "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='clients';")
if [ "$HAS_CLIENTS" != "0" ]; then
  COLS=$({S} "$SQLITE_BIN" "$DB" "SELECT group_concat(name, ',') FROM pragma_table_info('clients') WHERE name NOT IN ('id','inbound_id');")
  if [ -z "$COLS" ]; then
    echo "ERR_NO_CLIENTS_TABLE"
    exit 11
  fi

  HAS_UUID=$({S} "$SQLITE_BIN" "$DB" "SELECT COUNT(*) FROM pragma_table_info('clients') WHERE name='uuid';")
  if [ "$HAS_UUID" = "0" ]; then
    echo "ERR_NO_UUID"
    exit 12
  fi

  SELS=$(echo "$COLS" | awk -F',' '{{for(i=1;i<=NF;i++){{printf "c.%s", $i; if(i<NF) printf ","}}}}')
  BEFORE=$({S} "$SQLITE_BIN" "$DB" "SELECT COUNT(*) FROM clients WHERE inbound_id=$TARGET_ID;")

  {S} "$SQLITE_BIN" "$DB" "BEGIN;
    INSERT INTO clients (inbound_id, $COLS)
    SELECT $TARGET_ID, $SELS
    FROM clients c
    WHERE c.inbound_id IN ($SRC_IDS)
      AND c.uuid NOT IN (SELECT uuid FROM clients WHERE inbound_id=$TARGET_ID);
    COMMIT;"

  AFTER=$({S} "$SQLITE_BIN" "$DB" "SELECT COUNT(*) FROM clients WHERE inbound_id=$TARGET_ID;")
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
        return {{}}
    try:
        return json.loads(s)
    except Exception:
        return {{}}

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
print(f"OK_MODE=JSON OK_ADDED={{added}} TARGET_CLIENTS={{len(tclients)}} SETTINGS_COL={{settings_col}}")
PY
"""

# ------------------------- States -------------------------
IP, SSH_USER, SSH_PASS, SSH_PORT, TARGET_ID, SRC_COUNT, SRC_IDS, CONFIRM = range(8)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")

async def start_merge_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text("📌 IPv4 سرور را ارسال کنید:")
    return IP

async def got_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = (update.message.text or "").strip()
    if not is_ipv4(ip):
        await update.message.reply_text("❌ IPv4 معتبر نیست. مثال: 159.65.243.137")
        return IP
    context.user_data["ip"] = ip
    await update.message.reply_text("👤 یوزرنیم SSH را ارسال کنید (اگر root هستی /skip بزن):")
    return SSH_USER

async def got_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    user = "root" if txt == "/skip" else txt.strip()
    if not user:
        await update.message.reply_text("❌ یوزرنیم خالیه.")
        return SSH_USER
    context.user_data["ssh_user"] = user
    await update.message.reply_text("🔑 پسورد SSH را ارسال کنید:")
    return SSH_PASS

async def got_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = (update.message.text or "").strip()
    if not pw:
        await update.message.reply_text("❌ پسورد خالیه.")
        return SSH_PASS
    context.user_data["ssh_pass"] = pw
    await update.message.reply_text("🔢 پورت SSH را ارسال کنید (اگر 22 هست /skip بزن):")
    return SSH_PORT

async def got_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    port = 22 if txt == "/skip" else parse_int(txt, 1, 65535)
    if port is None:
        await update.message.reply_text("❌ پورت معتبر نیست (1..65535).")
        return SSH_PORT
    context.user_data["ssh_port"] = port
    await update.message.reply_text("🎯 Target Inbound ID را ارسال کنید:")
    return TARGET_ID

async def got_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_int(update.message.text, 1, 10**9)
    if v is None:
        await update.message.reply_text("❌ فقط عدد بفرست. مثال: 12")
        return TARGET_ID
    context.user_data["target_id"] = v
    await update.message.reply_text("🔢 چندتا Source Inbound دارید؟ (1 تا 30)")
    return SRC_COUNT

async def got_src_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = parse_int(update.message.text, 1, 30)
    if n is None:
        await update.message.reply_text("❌ عدد معتبر بین 1 تا 30 بفرست.")
        return SRC_COUNT
    context.user_data["src_count"] = n
    context.user_data["src_ids"] = []
    await update.message.reply_text("📥 Source ID شماره 1 را بفرست:")
    return SRC_IDS

async def got_src_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = parse_int(update.message.text, 1, 10**9)
    if sid is None:
        await update.message.reply_text("❌ فقط عدد بفرست.")
        return SRC_IDS

    src_ids: List[int] = context.user_data.get("src_ids", [])
    src_ids.append(sid)
    context.user_data["src_ids"] = src_ids

    n = int(context.user_data["src_count"])
    if len(src_ids) < n:
        await update.message.reply_text(f"✅ ثبت شد. Source ID شماره {len(src_ids)+1} را بفرست:")
        return SRC_IDS

    await update.message.reply_text(
        "🧾 خلاصه:\n"
        f"Server: {context.user_data['ip']}:{context.user_data['ssh_port']}\n"
        f"SSH User: {context.user_data['ssh_user']}\n"
        f"Target: {context.user_data['target_id']}\n"
        f"Sources: {', '.join(str(x) for x in src_ids)}\n\n"
        "اگر مطمئنی انجام بده ✅",
        reply_markup=kb_confirm(),
    )
    return CONFIRM

async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("✅ لغو شد. /start بزن برای شروع دوباره.")
        return ConversationHandler.END

    ip = context.user_data["ip"]
    ssh_user = context.user_data["ssh_user"]
    ssh_pass = context.user_data["ssh_pass"]
    ssh_port = context.user_data["ssh_port"]
    target_id = int(context.user_data["target_id"])
    src_ids = [int(x) for x in context.user_data.get("src_ids", [])]

    await q.edit_message_text("⏳ اتصال به سرور...")

    try:
        # 1) Find DB
        fcmd = find_db_cmd(ssh_user)
        code, out, err = await asyncio.wait_for(
            asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, fcmd, 20, 60),
            timeout=60,
        )
        db_path = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        if code != 0 or not db_path or "NOT_FOUND" in db_path:
            await q.message.reply_text("❌ دیتابیس پیدا نشد یا دسترسی ندارم.")
            await q.message.reply_text(_short(out + "\n" + err))
            context.user_data.clear()
            return ConversationHandler.END

        await q.message.reply_text(f"✅ دیتابیس: {db_path}")

        # 2) Preflight sqlite check (خیلی مهم برای تشخیص دقیق)
        pref = r"""
set -e
echo "sqlite3_path=$(command -v sqlite3 || true)"
echo "ls_usr_bin=$(ls -l /usr/bin/sqlite3 2>/dev/null || true)"
echo "ls_bin=$(ls -l /bin/sqlite3 2>/dev/null || true)"
echo "ver=$(sqlite3 --version 2>/dev/null || true)"
"""
        codep, outp, errp = await asyncio.wait_for(
            asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, pref, 20, 60),
            timeout=40,
        )
        if codep == 0:
            await q.message.reply_text("🔎 بررسی sqlite3:\n" + _short(outp, 1200))

        # 3) Merge
        await q.message.reply_text("🧩 در حال اجرای Merge ...")
        src_csv = ",".join(str(x) for x in src_ids)
        merge_script = make_merge_script(ssh_user)

        remote_cmd = f"""
set -e
TMP=/tmp/xuihub_merge.sh
cat > "$TMP" <<'EOS'
{merge_script}
EOS
chmod +x "$TMP"
"$TMP" "{db_path}" "{target_id}" "{src_csv}"
"""

        code2, out2, err2 = await asyncio.wait_for(
            asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, remote_cmd, 20, 120),
            timeout=180,
        )

        if code2 != 0:
            msg = (out2 + "\n" + err2).strip()
            if "ERR_SUDO_NEEDS_PASSWORD" in msg:
                await q.message.reply_text("❌ مشکل: sudo بدون پسورد نیست (برای یوزر غیر root).")
            elif "ERR_NO_SQLITE3" in msg:
                await q.message.reply_text("❌ مشکل: sqlite3 روی مسیرهای معمول پیدا نشد یا اجرا نشد.")
            else:
                await q.message.reply_text("❌ Merge ناموفق شد.")
            await q.message.reply_text(_short(msg, 3500))
            context.user_data.clear()
            return ConversationHandler.END

        await q.message.reply_text("🎉 ادغام انجام شد ✅")
        await q.message.reply_text(_short(out2, 3500))
        context.user_data.clear()
        await q.message.reply_text("برای ادغام بعدی /start را بزن ✅", reply_markup=kb_main())
        return ConversationHandler.END

    except asyncio.TimeoutError:
        await q.message.reply_text("❌ Timeout: عملیات طولانی شد یا سرور پاسخ نداد.")
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        logger.exception("merge crashed")
        await q.message.reply_text(f"❌ خطای غیرمنتظره: {e}")
        context.user_data.clear()
        return ConversationHandler.END

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
    try:
        if isinstance(update, Update):
            if update.message:
                await update.message.reply_text("⚠️ خطای داخلی رخ داد. دوباره تلاش کن.")
            elif update.callback_query:
                await update.callback_query.message.reply_text("⚠️ خطای داخلی رخ داد. دوباره تلاش کن.")
    except Exception:
        pass

def main():
    token = get_token()
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_merge_cb, pattern="^start_merge$")],
        states={
            IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_ip)],
            SSH_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_ssh_user)],
            SSH_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_ssh_pass)],
            SSH_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_ssh_port)],
            TARGET_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_target_id)],
            SRC_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_src_count)],
            SRC_IDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_src_id)],
            CONFIRM: [CallbackQueryHandler(confirm_cb, pattern="^(do_merge|cancel)$")],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_error_handler(on_error)
    app.run_polling()

if __name__ == "__main__":
    main()
