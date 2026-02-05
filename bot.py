import os
import json
import re
import asyncio
import tempfile
from enum import IntEnum, auto
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple

import paramiko
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    BotCommand,
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

def ssh_exec(host: str, port: int, user: str, password: str, cmd: str, timeout: int = 25):
    c = ssh_client(host, port, user, password, timeout=timeout)
    _, stdout, stderr = c.exec_command(cmd, get_pty=True)
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    code = stdout.channel.recv_exit_status()
    c.close()
    return code, out, err

async def ssh_run_cmd(ssh: Dict[str, Any], cmd: str):
    return await asyncio.to_thread(
        ssh_exec,
        ssh["ssh_host"], ssh["ssh_port"], ssh["ssh_user"], ssh["ssh_pass"],
        cmd
    )

# ---------------- x-ui DB helpers (export only) ----------------
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

async def find_db_path(ssh: Dict[str, Any]) -> Optional[str]:
    code, out, err = await ssh_run_cmd(ssh, FIND_DB_CMD)
    db_path = out.strip().splitlines()[-1] if out.strip() else ""
    if "NOT_FOUND" in db_path or not db_path:
        return None
    return db_path

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
        f"🤖 xui_HUB\n"
        f"👨‍💻 Developer: @EmadHabibnia"
    )

# ---------------- UI Texts ----------------
RLM = "\u200F"  # Right-to-left mark

START_TEXT = (
    f"{RLM}🤖 **خوش آمدید! xuiHUB**\n\n"
    f"{RLM}xuiHUB یک ربات حرفه‌ای برای مدیریت سرورها و کنترل پنل‌های **3x-ui / x-ui** است.\n\n"
    f"{RLM}از داخل تلگرام می‌توانید:\n"
    f"{RLM}• سرورها را اضافه / ویرایش / حذف کنید\n"
    f"{RLM}• پورت‌ها و کانفیگ‌ها را مدیریت کنید\n"
    f"{RLM}• بکاپ بگیرید یا بکاپ را وارد کنید\n"
    f"{RLM}• نصب و پیکربندی‌های ضروری را اجرا کنید\n\n"
    f"{RLM}برای شروع از منوی زیر استفاده کنید 👇\n\n"
    f"{RLM}👨‍💻 توسعه‌دهنده: @EmadHabibnia"
)

def one_line_hint(text: str) -> str:
    return f"ℹ️ {text}"

# ---------------- Keyboards ----------------
def kb_main() -> InlineKeyboardMarkup:
    # ✅ طبق خواسته: مدیریت سرورها تک دکمه در یک ردیف کامل
    # بقیه دکمه‌ها دوتایی کنار هم
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛠 مدیریت سرورها", callback_data="manage_servers")],
        [
            InlineKeyboardButton("🔀 مدیریت پورت و کانفیگ", callback_data="merge_menu"),
            InlineKeyboardButton("🗂 مدیریت بکاپ", callback_data="backup_menu"),
        ],
        [
            InlineKeyboardButton("⚙️ نصب و پیکربندی", callback_data="setup_menu"),
            InlineKeyboardButton("📌 راهنما", callback_data="help_menu"),
        ],
    ])

def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])

def display_server_name(s: Dict[str, Any]) -> str:
    panel = s.get("panel") or {}
    host = (panel.get("panel_host") or "").strip()
    return host or s.get("ssh_host", "server")

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
    rows.append([InlineKeyboardButton("⬅️ بازگشت به منو", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_backup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 گرفتن بکاپ", callback_data="bk_export")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")],
    ])

# ---------------- States ----------------
class S(IntEnum):
    ADD_SRV_HOST = auto()
    ADD_SRV_SSH_USER = auto()
    ADD_SRV_SSH_PASS = auto()
    ADD_SRV_SSH_PORT = auto()
    ADD_SRV_HAS_PANEL = auto()
    ADD_SRV_PANEL_HOST = auto()
    ADD_SRV_PANEL_PORT = auto()
    ADD_SRV_PANEL_PATH = auto()
    ADD_SRV_PANEL_SCHEME = auto()

    EDIT_SERVER_FIELD = auto()

    BK_EXPORT_PICK = auto()

# ---------------- /start ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")

# ---------------- Startup Commands list ----------------
async def post_init(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "شروع ربات و نمایش منو"),
        ])
    except:
        pass

# ---------------- Router (fix: buttons not working) ----------------
async def router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data
    store = load_store()
    uid = q.from_user.id
    bucket = get_user_bucket(store, uid)

    if data == "back_main":
        await q.edit_message_text(START_TEXT, reply_markup=kb_main(), parse_mode="Markdown")
        return

    if data == "help_menu":
        await q.edit_message_text(
            "📌 **راهنما**\n\n"
            "• 🛠 مدیریت سرورها: افزودن/ویرایش/حذف سرورها\n"
            "• 🔀 مدیریت پورت و کانفیگ: (در نسخه بعدی تکمیل می‌شود)\n"
            "• 🗂 مدیریت بکاپ: گرفتن بکاپ دیتابیس x-ui\n"
            "• ⚙️ نصب و پیکربندی: (در نسخه بعدی تکمیل می‌شود)\n\n"
            "👨‍💻 Developer: @EmadHabibnia",
            reply_markup=kb_back_main(),
            parse_mode="Markdown"
        )
        return

    if data == "setup_menu":
        await q.edit_message_text(
            "⚙️ **نصب و پیکربندی**\n\n"
            "این بخش برای اجرای عملیات‌های نصب/آپدیت روی سرورها از طریق SSH است.\n"
            f"{one_line_hint('در نسخه بعدی: لیست عملیات + اجرا روی سرور انتخابی اضافه می‌شود.')}",
            reply_markup=kb_back_main(),
            parse_mode="Markdown"
        )
        return

    if data == "manage_servers":
        await q.edit_message_text(
            "🛠 **مدیریت سرورها**\n\n"
            "در این بخش می‌توانید سرورهای خود را اضافه کنید و در صورت نیاز اطلاعات پنل x-ui را هم ثبت کنید.\n"
            f"{one_line_hint('فقط اطلاعات سرور ذخیره می‌شود.')}",
            reply_markup=kb_servers_list(store, uid),
            parse_mode="Markdown"
        )
        return

    if data.startswith("del_server:"):
        sid = data.split(":", 1)[1]
        if sid in bucket["servers"]:
            del bucket["servers"][sid]
            bucket["order"] = [x for x in bucket["order"] if x != sid]
            save_store(store)
        await q.edit_message_text("✅ سرور حذف شد.", reply_markup=kb_servers_list(store, uid))
        return

    if data.startswith("edit_server:"):
        sid = data.split(":", 1)[1]
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

    if data == "backup_menu":
        await q.edit_message_text(
            "🗂 **مدیریت بکاپ**\n\n"
            "📤 با انتخاب «گرفتن بکاپ»، فایل دیتابیس x-ui همین لحظه از سرور دانلود می‌شود و در تلگرام ارسال خواهد شد.\n\n"
            f"{one_line_hint('برای گرفتن بکاپ باید x-ui نصب باشد و کاربر SSH دسترسی sudo داشته باشد.')}",
            reply_markup=kb_backup_menu(),
            parse_mode="Markdown"
        )
        return

    if data == "bk_export":
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

    if data.startswith("bk_export:"):
        sid = data.split(":", 1)[1]
        srv = bucket["servers"].get(sid)
        if not srv:
            await q.edit_message_text("سرور پیدا نشد.", reply_markup=kb_main())
            return ConversationHandler.END

        ssh = {
            "ssh_host": srv["ssh_host"],
            "ssh_user": srv["ssh_user"],
            "ssh_pass": srv["ssh_pass"],
            "ssh_port": int(srv["ssh_port"]),
        }

        await q.edit_message_text("⏳ در حال گرفتن بکاپ...")

        db_path = await find_db_path(ssh)
        if not db_path:
            await q.edit_message_text("❌ دیتابیس x-ui.db پیدا نشد یا sudo ندارم.", reply_markup=kb_main())
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
            await q.message.reply_document(document=InputFile(local_path, filename=filename), caption=caption)
            await q.message.reply_text("✅ انجام شد.", reply_markup=kb_main())
        finally:
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except:
                pass
        return ConversationHandler.END

    # merge_menu placeholder (for now)
    if data == "merge_menu":
        await q.edit_message_text(
            "🔀 **مدیریت پورت و کانفیگ**\n\n"
            "این بخش برای ادغام کلاینت‌ها و مدیریت کانفیگ‌هاست.\n"
            f"{one_line_hint('در نسخه بعدی تکمیل می‌شود.')}",
            reply_markup=kb_back_main(),
            parse_mode="Markdown"
        )
        return

    # fallback
    await q.edit_message_text("گزینه نامعتبر.", reply_markup=kb_main())

# ---------------- Add Server Flow (entry) ----------------
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

    store = load_store()
    uid = update.effective_user.id
    bucket = get_user_bucket(store, uid)

    srv = context.user_data["new_server"]
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

    await update.message.reply_text("✅ سرور اضافه شد.", reply_markup=kb_main())
    return ConversationHandler.END

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

    allowed = {"ssh_host", "ssh_user", "ssh_pass", "ssh_port"}
    if key not in allowed:
        await update.message.reply_text("❌ فیلد معتبر نیست. مثال: ssh_port=22")
        return S.EDIT_SERVER_FIELD

    store = load_store()
    bucket = get_user_bucket(store, update.effective_user.id)
    srv = bucket["servers"].get(sid)
    if not srv:
        context.user_data.clear()
        await update.message.reply_text("❌ سرور پیدا نشد.", reply_markup=kb_main())
        return ConversationHandler.END

    if key == "ssh_port":
        try:
            n = int(val)
            if not (1 <= n <= 65535):
                raise ValueError()
            val = n
        except:
            await update.message.reply_text("❌ پورت معتبر ارسال کنید (1..65535).")
            return S.EDIT_SERVER_FIELD

    srv[key] = val
    save_store(store)
    context.user_data.clear()

    await update.message.reply_text("✅ ویرایش انجام شد.", reply_markup=kb_main())
    return ConversationHandler.END

# ---------------- Main ----------------
def main():
    token = env_required("TOKEN")
    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))

    conv_add_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_server_entry, pattern="^add_server$")],
        states={
            S.ADD_SRV_HOST: [MessageHandler(filters.TEXT, add_srv_host)],
            S.ADD_SRV_SSH_USER: [MessageHandler(filters.TEXT, add_srv_ssh_user)],
            S.ADD_SRV_SSH_PASS: [MessageHandler(filters.TEXT, add_srv_ssh_pass)],
            S.ADD_SRV_SSH_PORT: [MessageHandler(filters.TEXT, add_srv_ssh_port)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_add_server)

    conv_edit_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(router, pattern=r"^edit_server:")],
        states={
            S.EDIT_SERVER_FIELD: [MessageHandler(filters.TEXT, edit_server_field)]
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(conv_edit_server)

    # ✅ مهم: router باید آخر اضافه شود تا همه دکمه‌ها کار کنند
    app.add_handler(CallbackQueryHandler(router))

    app.run_polling()

if __name__ == "__main__":
    main()
