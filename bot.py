import os
import json
import re
import asyncio
from typing import Dict, Any, Optional, Tuple, List

import paramiko
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters
)

STORE_FILE = "store.json"

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
    if "users" not in store:
        store["users"] = {}
    if uid not in store["users"]:
        store["users"][uid] = {"panels": {}, "order": []}
    return store["users"][uid]

def safe_panel_id(host: str) -> str:
    pid = re.sub(r"[^a-zA-Z0-9_.-]+", "_", host.strip())
    return pid or "panel"

def ssh_exec(host: str, port: int, user: str, password: str, cmd: str, timeout: int = 25) -> Tuple[int, str, str]:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, port=port, username=user, password=password, timeout=timeout)
    stdin, stdout, stderr = client.exec_command(cmd, get_pty=True)
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    code = stdout.channel.recv_exit_status()
    client.close()
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

def make_merge_script() -> str:
    # merges clients into target inbound, prevents uuid duplicates
    return r"""
set -e
DB="$1"
TARGET_ID="$2"
SRC_IDS="$3"

command -v sqlite3 >/dev/null 2>&1 || { echo "ERR_NO_SQLITE3"; exit 10; }

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
echo "OK_ADDED=$ADDED BEFORE=$BEFORE AFTER=$AFTER"
"""

# ----------------- Telegram states -----------------
(
    ADD_IP, ADD_HTTP, ADD_PANEL_PORT, ADD_PATH, ADD_USER, ADD_PASS,
    ADD_SSH_HOST, ADD_SSH_USER, ADD_SSH_PORT, ADD_SSH_PASS,
    MERGE_COUNT, MERGE_PORTS, MERGE_TARGET, MERGE_CONFIRM,
    EDIT_CHOOSE_FIELD, EDIT_VALUE
) = range(16)

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛠 مدیریت پنل‌ها", callback_data="manage_panels")]])

def kb_panels(store: Dict[str, Any], user_id: int) -> InlineKeyboardMarkup:
    bucket = get_user_bucket(store, user_id)
    rows = [[InlineKeyboardButton("➕ اضافه کردن پنل", callback_data="add_panel")]]
    for pid in bucket.get("order", []):
        rows.append([
            InlineKeyboardButton(f"📌 {pid}", callback_data=f"panel:{pid}"),
            InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit:{pid}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"del:{pid}")
        ])
    rows.append([InlineKeyboardButton("⬅️ برگشت", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_panel_actions(pid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔀 ادغام کردن پورت‌ها", callback_data=f"merge:{pid}")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="manage_panels")]
    ])

def kb_edit_fields(pid: str) -> InlineKeyboardMarkup:
    # کاربر انتخاب می‌کند کدام فیلد را ویرایش کند
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("پنل: IP/دامنه", callback_data=f"ef:{pid}:panel_host")],
        [InlineKeyboardButton("HTTP/HTTPS", callback_data=f"ef:{pid}:panel_scheme")],
        [InlineKeyboardButton("پورت پنل", callback_data=f"ef:{pid}:panel_port")],
        [InlineKeyboardButton("پچ پنل", callback_data=f"ef:{pid}:panel_path")],
        [InlineKeyboardButton("یوزرنیم پنل", callback_data=f"ef:{pid}:panel_user")],
        [InlineKeyboardButton("پسورد پنل", callback_data=f"ef:{pid}:panel_pass")],
        [InlineKeyboardButton("SSH Host", callback_data=f"ef:{pid}:ssh_host")],
        [InlineKeyboardButton("SSH User", callback_data=f"ef:{pid}:ssh_user")],
        [InlineKeyboardButton("SSH Port", callback_data=f"ef:{pid}:ssh_port")],
        [InlineKeyboardButton("SSH Pass", callback_data=f"ef:{pid}:ssh_pass")],
        [InlineKeyboardButton("⬅️ برگشت", callback_data="manage_panels")]
    ])

def field_label(key: str) -> str:
    m = {
        "panel_host":"IP/دامنه پنل",
        "panel_scheme":"HTTP/HTTPS",
        "panel_port":"پورت پنل",
        "panel_path":"پچ پنل",
        "panel_user":"یوزرنیم پنل",
        "panel_pass":"پسورد پنل",
        "ssh_host":"IP/دامنه سرور (SSH)",
        "ssh_user":"یوزرنیم SSH",
        "ssh_port":"پورت SSH",
        "ssh_pass":"پسورد SSH",
    }
    return m.get(key, key)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("xui_HUB آماده است ✅", reply_markup=kb_main())

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    store = load_store()
    user_id = update.effective_user.id

    if q.data == "back_main":
        await q.edit_message_text("منوی اصلی:", reply_markup=kb_main())
        return ConversationHandler.END

    if q.data == "manage_panels":
        await q.edit_message_text("مدیریت پنل‌ها:", reply_markup=kb_panels(store, user_id))
        return ConversationHandler.END

    if q.data == "add_panel":
        context.user_data.clear()
        context.user_data["new_panel"] = {}
        await q.edit_message_text("۱) آیپی یا دامنه پنل را بفرست:")
        return ADD_IP

    # انتخاب پنل
    if q.data.startswith("panel:"):
        pid = q.data.split(":", 1)[1]
        bucket = get_user_bucket(store, user_id)
        if pid not in bucket["panels"]:
            await q.edit_message_text("پنل پیدا نشد.", reply_markup=kb_panels(store, user_id))
            return ConversationHandler.END
        context.user_data["selected_pid"] = pid
        await q.edit_message_text(f"پنل انتخاب شد: {pid}", reply_markup=kb_panel_actions(pid))
        return ConversationHandler.END

    # حذف پنل
    if q.data.startswith("del:"):
        pid = q.data.split(":", 1)[1]
        bucket = get_user_bucket(store, user_id)
        if pid in bucket["panels"]:
            del bucket["panels"][pid]
            bucket["order"] = [x for x in bucket["order"] if x != pid]
            save_store(store)
        await q.edit_message_text("✅ حذف شد.", reply_markup=kb_panels(store, user_id))
        return ConversationHandler.END

    # ویرایش پنل → انتخاب فیلد
    if q.data.startswith("edit:"):
        pid = q.data.split(":", 1)[1]
        bucket = get_user_bucket(store, user_id)
        if pid not in bucket["panels"]:
            await q.edit_message_text("پنل پیدا نشد.", reply_markup=kb_panels(store, user_id))
            return ConversationHandler.END
        context.user_data.clear()
        context.user_data["edit_pid"] = pid
        await q.edit_message_text(f"✏️ ویرایش پنل: {pid}\nیک فیلد را انتخاب کن:", reply_markup=kb_edit_fields(pid))
        return EDIT_CHOOSE_FIELD

    # انتخاب فیلد برای ویرایش
    if q.data.startswith("ef:"):
        _, pid, key = q.data.split(":", 2)
        context.user_data["edit_pid"] = pid
        context.user_data["edit_key"] = key
        await q.edit_message_text(f"مقدار جدید برای «{field_label(key)}» را بفرست:")
        return EDIT_VALUE

    # ادغام
    if q.data.startswith("merge:"):
        pid = q.data.split(":", 1)[1]
        bucket = get_user_bucket(store, user_id)
        if pid not in bucket["panels"]:
            await q.edit_message_text("پنل پیدا نشد.", reply_markup=kb_panels(store, user_id))
            return ConversationHandler.END

        context.user_data.clear()
        context.user_data["merge"] = {"panel_id": pid, "ports": []}
        await q.edit_message_text(
            "🔀 ادغام پورت‌ها\n\n"
            "⚠️ پورت مقصد را خودتان از قبل داخل پنل ساخته باشید.\n\n"
            "تعداد پورت‌های ورودی را بفرست (مثلاً 2):"
        )
        return MERGE_COUNT

    return ConversationHandler.END

# ---- Add panel flow ----
async def add_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["panel_host"] = update.message.text.strip()
    await update.message.reply_text("۲) نوع پنل؟ HTTP یا HTTPS")
    return ADD_HTTP

async def add_http(update: Update, context: ContextTypes.DEFAULT_TYPE):
    v = update.message.text.strip().lower()
    if v not in ("http", "https"):
        await update.message.reply_text("فقط HTTP یا HTTPS بفرست.")
        return ADD_HTTP
    context.user_data["new_panel"]["panel_scheme"] = v
    await update.message.reply_text("۳) پورت پنل؟ (مثلاً 2053)")
    return ADD_PANEL_PORT

async def add_panel_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("پورت معتبر بفرست (1..65535).")
        return ADD_PANEL_PORT
    context.user_data["new_panel"]["panel_port"] = port
    await update.message.reply_text("۴) پچ پنل (مثلاً /panel). اگر نداری / بفرست:")
    return ADD_PATH

async def add_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = update.message.text.strip()
    if not path.startswith("/"):
        path = "/" + path
    context.user_data["new_panel"]["panel_path"] = path
    await update.message.reply_text("۵) یوزرنیم پنل:")
    return ADD_USER

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["panel_user"] = update.message.text.strip()
    await update.message.reply_text("۶) پسورد پنل:")
    return ADD_PASS

async def add_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["panel_pass"] = update.message.text.strip()
    await update.message.reply_text("۷) آیپی سرور خارج (SSH Host):")
    return ADD_SSH_HOST

async def add_ssh_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["ssh_host"] = update.message.text.strip()
    await update.message.reply_text("۸) یوزرنیم سرور (SSH User):")
    return ADD_SSH_USER

async def add_ssh_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_panel"]["ssh_user"] = update.message.text.strip()
    await update.message.reply_text("۹) پورت SSH:")
    return ADD_SSH_PORT

async def add_ssh_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("پورت SSH معتبر بفرست.")
        return ADD_SSH_PORT
    context.user_data["new_panel"]["ssh_port"] = port
    await update.message.reply_text("۱۰) پسورد SSH:")
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

    context.user_data.clear()  # پاک شدن دیتاهای موقت
    await update.message.reply_text("✅ پنل ذخیره شد. /start")
    return ConversationHandler.END

# ---- Edit flow ----
async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    pid = context.user_data.get("edit_pid")
    key = context.user_data.get("edit_key")
    if not pid or not key or pid not in bucket["panels"]:
        context.user_data.clear()
        await update.message.reply_text("❌ خطا. دوباره از مدیریت پنل‌ها شروع کن. /start")
        return ConversationHandler.END

    val = update.message.text.strip()
    if key in ("panel_port", "ssh_port"):
        try:
            v = int(val)
            if not (1 <= v <= 65535):
                raise ValueError()
            val = v
        except:
            await update.message.reply_text("پورت معتبر بفرست (1..65535).")
            return EDIT_VALUE
    elif key == "panel_scheme":
        v = val.lower()
        if v not in ("http", "https"):
            await update.message.reply_text("فقط HTTP یا HTTPS بفرست.")
            return EDIT_VALUE
        val = v
    elif key == "panel_path":
        if not val.startswith("/"):
            val = "/" + val

    bucket["panels"][pid][key] = val
    save_store(store)

    context.user_data.clear()
    await update.message.reply_text("✅ ویرایش انجام شد. /start")
    return ConversationHandler.END

# ---- Merge flow ----
async def merge_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(update.message.text.strip())
        if not (1 <= n <= 30):
            raise ValueError()
    except:
        await update.message.reply_text("یک عدد معتبر بفرست (1 تا 30).")
        return MERGE_COUNT

    context.user_data["merge"]["count"] = n
    context.user_data["merge"]["ports"] = []
    await update.message.reply_text(f"{n} پورت ورودی را یکی‌یکی بفرست. (پورت 1):")
    return MERGE_PORTS

async def merge_ports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("پورت معتبر بفرست.")
        return MERGE_PORTS

    m["ports"].append(port)
    idx = len(m["ports"])
    if idx < m["count"]:
        await update.message.reply_text(f"پورت {idx} ثبت شد ✅\nپورت بعدی (پورت {idx+1}):")
        return MERGE_PORTS

    await update.message.reply_text("✅ همه پورت‌های ورودی ثبت شد.\nحالا پورت مقصد را بفرست (مثلاً 443):")
    return MERGE_TARGET

async def merge_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = context.user_data["merge"]
    try:
        port = int(update.message.text.strip())
        if not (1 <= port <= 65535):
            raise ValueError()
    except:
        await update.message.reply_text("پورت مقصد معتبر بفرست.")
        return MERGE_TARGET

    m["target_port"] = port
    await update.message.reply_text(
        "🧾 خلاصه:\n"
        f"پورت‌های ورودی: {m['ports']}\n"
        f"پورت مقصد: {m['target_port']}\n\n"
        "برای اجرای عملیات بنویس: OK"
    )
    return MERGE_CONFIRM

async def merge_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip().lower() != "ok":
        await update.message.reply_text("اگر می‌خوای انجام بشه فقط بنویس: OK")
        return MERGE_CONFIRM

    store = load_store()
    user_id = update.effective_user.id
    bucket = get_user_bucket(store, user_id)

    pid = context.user_data["merge"]["panel_id"]
    panel = bucket["panels"].get(pid)
    if not panel:
        context.user_data.clear()
        await update.message.reply_text("❌ پنل پیدا نشد. /start")
        return ConversationHandler.END

    src_ports = context.user_data["merge"]["ports"]
    target_port = context.user_data["merge"]["target_port"]

    await update.message.reply_text("⏳ در حال اتصال به سرور و ادغام...")

    # find db
    code, out, err = await asyncio.to_thread(
        ssh_exec,
        panel["ssh_host"], panel["ssh_port"], panel["ssh_user"], panel["ssh_pass"],
        FIND_DB_CMD
    )
    db_path = out.strip().splitlines()[-1] if out.strip() else ""
    if "NOT_FOUND" in db_path or not db_path:
        context.user_data.clear()
        await update.message.reply_text("❌ دیتابیس x-ui.db پیدا نشد یا دسترسی sudo ندارم.")
        return ConversationHandler.END

    def get_inbound_id(port: int) -> Optional[int]:
        c, o, e = ssh_exec(panel["ssh_host"], panel["ssh_port"], panel["ssh_user"], panel["ssh_pass"],
                          inbound_id_by_port_cmd(db_path, port))
        v = o.strip()
        if not v:
            return None
        try:
            return int(v)
        except:
            return None

    target_id = await asyncio.to_thread(get_inbound_id, target_port)
    if not target_id:
        context.user_data.clear()
        await update.message.reply_text(f"❌ inbound با پورت مقصد {target_port} پیدا نشد. اول داخل پنل بساز.")
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
        await update.message.reply_text(f"❌ این پورت‌ها inbound ندارند/پیدا نشدند: {missing}")
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
        panel["ssh_host"], panel["ssh_port"], panel["ssh_user"], panel["ssh_pass"],
        remote_cmd
    )
    if code != 0:
        context.user_data.clear()
        msg = (out + "\n" + err).strip()
        await update.message.reply_text(f"❌ خطا:\n{msg[:3500]}")
        return ConversationHandler.END

    await asyncio.to_thread(
        ssh_exec,
        panel["ssh_host"], panel["ssh_port"], panel["ssh_user"], panel["ssh_pass"],
        "sudo x-ui restart || sudo systemctl restart x-ui || true"
    )

    context.user_data.clear()  # پاک شدن دیتاهای موقت
    await update.message.reply_text(f"✅ ادغام انجام شد.\n{out.strip()}\n\n/start")
    return ConversationHandler.END

def env_required(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env: {name}")
    return v

def main():
    token = env_required("TOKEN")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))

    conv_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern="^add_panel$")],
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

    conv_edit = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r"^edit:") , CallbackQueryHandler(on_callback, pattern=r"^ef:")],
        states={
            EDIT_CHOOSE_FIELD: [CallbackQueryHandler(on_callback, pattern=r"^ef:")],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_edit)

    conv_merge = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_callback, pattern=r"^merge:")],
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

    app.run_polling()

if __name__ == "__main__":
    main()
