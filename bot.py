import os
import re
import asyncio
import tempfile
import logging
from typing import List, Optional, Tuple

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

# ------------------------- Logging -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("xuihub_merge_simple")


# ------------------------- .env loader (optional) -------------------------
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


# ------------------------- Text/UI -------------------------
START_TEXT = (
    "🤖 به **xuiHUB** خوش آمدید\n\n"
    "این ربات یک کار ساده انجام می‌دهد:\n"
    "🔀 ادغام کلاینت‌های چند Inbound داخل یک Inbound مقصد روی همان سرور\n\n"
    "✅ فقط اطلاعات سرور و شماره Inboundها را می‌گیرد و ادغام می‌کند.\n"
    "⛔️ هیچ Stop انجام نمی‌دهد و اگر خطا باشد دقیق گزارش می‌دهد.\n\n"
    "برای شروع روی دکمه زیر بزن 👇\n"
    "👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔀 شروع ادغام اینباند", callback_data="start_merge")],
    ])


def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ انجام بده", callback_data="do_merge"),
         InlineKeyboardButton("❌ لغو", callback_data="cancel")],
    ])


# ------------------------- Validators -------------------------
def is_ipv4(ip: str) -> bool:
    ip = (ip or "").strip()
    if not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", ip):
        return False
    try:
        parts = [int(x) for x in ip.split(".")]
        return all(0 <= p <= 255 for p in parts)
    except Exception:
        return False


def parse_int(s: str, min_v: int, max_v: int) -> Optional[int]:
    s = (s or "").strip()
    if not re.fullmatch(r"\d+", s):
        return None
    v = int(s)
    if not (min_v <= v <= max_v):
        return None
    return v


def _short(s: str, n: int = 1500) -> str:
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


# ------------------------- SSH helpers (robust) -------------------------
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


def ssh_exec(host: str, port: int, user: str, password: str, cmd: str,
             conn_timeout: int = 20, read_timeout: int = 35) -> Tuple[int, str, str]:
    c = ssh_client(host, port, user, password, timeout=conn_timeout)
    try:
        return ssh_exec_raw(c, cmd, read_timeout=read_timeout)
    finally:
        c.close()


# ------------------------- DB finder (fast) -------------------------
FIND_DB_CMD = r"""
set -e
for p in /etc/x-ui/x-ui.db /usr/local/x-ui/x-ui.db /opt/x-ui/x-ui.db /var/lib/x-ui/x-ui.db /root/x-ui.db; do
  if [ -f "$p" ]; then echo "$p"; exit 0; fi
done

if command -v timeout >/dev/null 2>&1; then
  DB=$(timeout 12s sudo -n find / -maxdepth 6 -name "x-ui.db" 2>/dev/null | head -n 1 || true)
else
  DB=$(sudo -n find / -maxdepth 6 -name "x-ui.db" 2>/dev/null | head -n 1 || true)
fi

if [ -z "$DB" ]; then
  echo "NOT_FOUND"
else
  echo "$DB"
fi
"""


def make_merge_script() -> str:
    # اسکریپت merge دقیقاً مثل نسخه‌ای که درست کار می‌کرد
    return r"""
set -e
DB="$1"
TARGET_ID="$2"
SRC_IDS="$3"

command -v sqlite3 >/dev/null 2>&1 || { echo "ERR_NO_SQLITE3"; exit 10; }
command -v python3 >/dev/null 2>&1 || { echo "ERR_NO_PYTHON3"; exit 13; }

sudo -n cp "$DB" "/tmp/xuihub_db_backup_$(date +%s).db" >/dev/null 2>&1 || true

HAS_CLIENTS=$(sudo -n sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='clients';")
if [ "$HAS_CLIENTS" != "0" ]; then
  COLS=$(sudo -n sqlite3 "$DB" "SELECT group_concat(name, ',') FROM pragma_table_info('clients') WHERE name NOT IN ('id','inbound_id');")
  if [ -z "$COLS" ]; then
    echo "ERR_NO_CLIENTS_TABLE"
    exit 11
  fi

  HAS_UUID=$(sudo -n sqlite3 "$DB" "SELECT COUNT(*) FROM pragma_table_info('clients') WHERE name='uuid';")
  if [ "$HAS_UUID" = "0" ]; then
    echo "ERR_NO_UUID"
    exit 12
  fi

  SELS=$(echo "$COLS" | awk -F',' '{for(i=1;i<=NF;i++){printf "c.%s", $i; if(i<NF) printf ","}}')
  BEFORE=$(sudo -n sqlite3 "$DB" "SELECT COUNT(*) FROM clients WHERE inbound_id=$TARGET_ID;")

  sudo -n sqlite3 "$DB" "BEGIN;
    INSERT INTO clients (inbound_id, $COLS)
    SELECT $TARGET_ID, $SELS
    FROM clients c
    WHERE c.inbound_id IN ($SRC_IDS)
      AND c.uuid NOT IN (SELECT uuid FROM clients WHERE inbound_id=$TARGET_ID);
    COMMIT;"

  AFTER=$(sudo -n sqlite3 "$DB" "SELECT COUNT(*) FROM clients WHERE inbound_id=$TARGET_ID;")
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


# ------------------------- States -------------------------
IP, SSH_USER, SSH_PASS, SSH_PORT, TARGET_ID, SRC_COUNT, SRC_IDS, CONFIRM = range(8)


# ------------------------- Error reporting -------------------------
async def send_error(update: Update, title: str, detail: str, extra: str = ""):
    msg = f"❌ {title}\n{detail}"
    if extra.strip():
        msg += f"\n\nجزئیات:\n{_short(extra, 1800)}"
    if update.message:
        await update.message.reply_text(msg)
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg)


# ------------------------- Handlers -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")


async def start_merge_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text("📌 لطفاً IPv4 سرور را ارسال کنید:")
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
    user = "root" if txt == "/skip" else txt
    if not user:
        await update.message.reply_text("❌ یوزرنیم خالیه. دوباره بفرست.")
        return SSH_USER
    context.user_data["ssh_user"] = user
    await update.message.reply_text("🔑 پسورد SSH را ارسال کنید:")
    return SSH_PASS


async def got_ssh_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pw = (update.message.text or "").strip()
    if not pw:
        await update.message.reply_text("❌ پسورد خالیه. دوباره بفرست.")
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
    await update.message.reply_text("🎯 شماره Inbound مقصد (Target ID) را ارسال کنید:")
    return TARGET_ID


async def got_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = parse_int((update.message.text or "").strip(), 1, 10**9)
    if v is None:
        await update.message.reply_text("❌ فقط عدد بفرست. مثال: 12")
        return TARGET_ID
    context.user_data["target_id"] = v
    await update.message.reply_text("🔢 چند تا Inbound ورودی دارید؟ (1 تا 30)")
    return SRC_COUNT


async def got_src_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = parse_int((update.message.text or "").strip(), 1, 30)
    if n is None:
        await update.message.reply_text("❌ عدد معتبر بین 1 تا 30 بفرست.")
        return SRC_COUNT
    context.user_data["src_count"] = n
    context.user_data["src_ids"] = []
    await update.message.reply_text("📥 حالا شماره Inbound ورودی 1 را ارسال کنید:")
    return SRC_IDS


async def got_src_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = parse_int((update.message.text or "").strip(), 1, 10**9)
    if sid is None:
        await update.message.reply_text("❌ فقط عدد بفرست.")
        return SRC_IDS

    src_ids: List[int] = context.user_data.get("src_ids", [])
    src_ids.append(sid)
    context.user_data["src_ids"] = src_ids

    n = int(context.user_data["src_count"])
    if len(src_ids) < n:
        await update.message.reply_text(f"✅ ثبت شد. حالا شماره Inbound ورودی {len(src_ids)+1} را بفرست:")
        return SRC_IDS

    # summary
    ip = context.user_data["ip"]
    user = context.user_data["ssh_user"]
    port = context.user_data["ssh_port"]
    target = context.user_data["target_id"]

    await update.message.reply_text(
        "🧾 خلاصه عملیات:\n\n"
        f"Server: {ip}:{port}\n"
        f"SSH User: {user}\n"
        f"Target Inbound ID: {target}\n"
        f"Source IDs: {', '.join(str(x) for x in src_ids)}\n\n"
        "اگر مطمئنی، روی «انجام بده» بزن ✅",
        reply_markup=kb_confirm(),
    )
    return CONFIRM


async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("✅ لغو شد. برای شروع دوباره /start را بزن.")
        return ConversationHandler.END

    if q.data != "do_merge":
        return CONFIRM

    ip = context.user_data["ip"]
    ssh_user = context.user_data["ssh_user"]
    ssh_pass = context.user_data["ssh_pass"]
    ssh_port = context.user_data["ssh_port"]
    target_id = int(context.user_data["target_id"])
    src_ids = [int(x) for x in context.user_data.get("src_ids", [])]

    await q.edit_message_text("⏳ شروع عملیات... اتصال به سرور")

    try:
        # 1) Find DB
        await q.message.reply_text("🔎 در حال پیدا کردن دیتابیس x-ui.db ...")
        try:
            code, out, err = await asyncio.wait_for(
                asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, FIND_DB_CMD, 20, 35),
                timeout=45,
            )
        except asyncio.TimeoutError:
            await send_error(update, "Timeout", "پیدا کردن دیتابیس طولانی شد/سرور پاسخ نداد.")
            context.user_data.clear()
            return ConversationHandler.END

        db_path = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        if code != 0:
            # خیلی وقتا به خاطر sudo -n است
            await send_error(update, "خطا در پیدا کردن دیتابیس",
                             "دستور اجرا شد ولی با خطا برگشت. احتمالاً sudo بدون پسورد نیست.",
                             out + "\n" + err)
            context.user_data.clear()
            return ConversationHandler.END

        if not db_path or "NOT_FOUND" in db_path:
            await send_error(update, "دیتابیس پیدا نشد",
                             "x-ui.db پیدا نشد یا دسترسی sudo ندارم.")
            context.user_data.clear()
            return ConversationHandler.END

        await q.message.reply_text(f"✅ دیتابیس پیدا شد:\n{db_path}")

        # 2) Run merge script
        await q.message.reply_text("🧩 در حال اجرای Merge ...")

        src_csv = ",".join(str(x) for x in src_ids)
        merge_script = make_merge_script()

        remote_cmd = f"""
set -e
TMP=/tmp/xuihub_merge.sh
cat > "$TMP" <<'EOS'
{merge_script}
EOS
chmod +x "$TMP"
sudo -n "$TMP" "{db_path}" "{target_id}" "{src_csv}"
"""

        try:
            code2, out2, err2 = await asyncio.wait_for(
                asyncio.to_thread(ssh_exec, ip, ssh_port, ssh_user, ssh_pass, remote_cmd, 20, 80),
                timeout=90,
            )
        except asyncio.TimeoutError:
            await send_error(update, "Timeout در Merge",
                             "اسکریپت ادغام طولانی شد یا گیر کرد.",
                             "احتمال: sqlite3 یا sudo یا قفل دیتابیس")
            context.user_data.clear()
            return ConversationHandler.END

        if code2 != 0:
            await send_error(update, "Merge ناموفق شد",
                             "کد خروجی غیر صفر برگشت.",
                             out2 + "\n" + err2)
            context.user_data.clear()
            return ConversationHandler.END

        # 3) Report result (بدون ریستارت سرویس)
        await q.message.reply_text(
            "🎉 ادغام انجام شد ✅\n\n"
            f"خروجی:\n{_short(out2, 3000)}"
        )

        context.user_data.clear()
        await q.message.reply_text("برای ادغام بعدی /start را بزن ✅", reply_markup=kb_main())
        return ConversationHandler.END

    except Exception as e:
        logger.exception("merge crashed")
        await send_error(update, "خطای غیرمنتظره", str(e))
        context.user_data.clear()
        return ConversationHandler.END


# ------------------------- Global error handler -------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
    try:
        if isinstance(update, Update):
            if update.message:
                await update.message.reply_text("⚠️ یک خطای داخلی رخ داد. دوباره تلاش کن.")
            elif update.callback_query:
                await update.callback_query.message.reply_text("⚠️ یک خطای داخلی رخ داد. دوباره تلاش کن.")
    except Exception:
        pass


# ------------------------- Main -------------------------
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
