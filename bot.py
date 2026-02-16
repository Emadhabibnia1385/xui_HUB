import os
import re
import json
import shutil
import sqlite3
import tempfile
import logging
from typing import List, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ------------------------- Logging -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("xui_db_merger")


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
        raise RuntimeError("TOKEN not found in environment or /opt/xui_HUB/.env")
    return token


# ------------------------- Telegram states -------------------------
UPLOAD_DB, ASK_TARGET, ASK_COUNT, ASK_SOURCES, CONFIRM = range(5)


# ------------------------- UI -------------------------
START_TEXT = (
    "🤖 به **xuiDB Merger** خوش آمدید\n\n"
    "این ربات برای ادغام کلاینت‌های چند Inbound داخل دیتابیس **x-ui.db** ساخته شده است.\n"
    "✅ فقط دیتابیس می‌گیرد، ادغام می‌کند، و دیتابیس جدید تحویل می‌دهد.\n"
    "⛔️ هیچ SSH و هیچ ریستارت سرویس انجام نمی‌دهد.\n\n"
    "از دکمه زیر شروع کنید 👇"
)


def kb_start() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ شروع ادغام دیتابیس", callback_data="start_merge_db")]]
    )


def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ انجام بده", callback_data="do_merge"),
          InlineKeyboardButton("❌ لغو", callback_data="cancel_merge")]]
    )


# ------------------------- Helpers -------------------------
def is_int_id(s: str) -> bool:
    s = (s or "").strip()
    return bool(re.fullmatch(r"\d+", s))


def short_err(e: Exception) -> str:
    msg = str(e).strip()
    return msg[:1500] + ("…" if len(msg) > 1500 else "")


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    cur = con.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;", (name,))
    return cur.fetchone() is not None


def get_inbounds_settings_col(con: sqlite3.Connection) -> Optional[str]:
    cur = con.cursor()
    cur.execute("PRAGMA table_info(inbounds);")
    cols = [r[1] for r in cur.fetchall()]
    for cand in ("settings", "setting", "settingsJson", "settings_json"):
        if cand in cols:
            return cand
    return None


def load_settings(con: sqlite3.Connection, settings_col: str, inbound_id: int) -> dict:
    cur = con.cursor()
    cur.execute(f"SELECT {settings_col} FROM inbounds WHERE id=?;", (inbound_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def save_settings(con: sqlite3.Connection, settings_col: str, inbound_id: int, obj: dict) -> None:
    s = json.dumps(obj, ensure_ascii=False)
    cur = con.cursor()
    cur.execute(f"UPDATE inbounds SET {settings_col}=? WHERE id=?;", (s, inbound_id))


def client_key(c: dict) -> Tuple[str, str]:
    for k in ("uuid", "id", "email", "password"):
        v = c.get(k)
        if isinstance(v, str) and v.strip():
            return (k, v.strip())
    return ("raw", json.dumps(c, sort_keys=True, ensure_ascii=False))


def merge_clients_table(con: sqlite3.Connection, target_id: int, source_ids: List[int]) -> int:
    cur = con.cursor()
    cur.execute("PRAGMA table_info(clients);")
    cols = [r[1] for r in cur.fetchall()]
    if "uuid" not in cols:
        raise RuntimeError("ستون uuid در جدول clients وجود ندارد.")

    cols_to_copy = [c for c in cols if c not in ("id", "inbound_id")]
    if not cols_to_copy:
        raise RuntimeError("ستون قابل انتقال در clients پیدا نشد.")

    cur.execute("SELECT COUNT(*) FROM clients WHERE inbound_id=?;", (target_id,))
    before = int(cur.fetchone()[0])

    cols_sql = ",".join(cols_to_copy)
    select_sql = ",".join([f"c.{c}" for c in cols_to_copy])

    src_placeholders = ",".join(["?"] * len(source_ids))
    sql = f"""
    INSERT INTO clients (inbound_id, {cols_sql})
    SELECT ?, {select_sql}
    FROM clients c
    WHERE c.inbound_id IN ({src_placeholders})
      AND c.uuid NOT IN (SELECT uuid FROM clients WHERE inbound_id=?);
    """

    con.execute("BEGIN;")
    cur.execute(sql, (target_id, *source_ids, target_id))
    con.execute("COMMIT;")

    cur.execute("SELECT COUNT(*) FROM clients WHERE inbound_id=?;", (target_id,))
    after = int(cur.fetchone()[0])
    return max(0, after - before)


def merge_clients_in_settings(con: sqlite3.Connection, target_id: int, source_ids: List[int]) -> int:
    settings_col = get_inbounds_settings_col(con)
    if not settings_col:
        raise RuntimeError("ستون settings در جدول inbounds پیدا نشد (settings/setting/...).")

    tset = load_settings(con, settings_col, target_id)
    tclients = tset.get("clients") or []
    if not isinstance(tclients, list):
        tclients = []

    existing = set()
    for c in tclients:
        if isinstance(c, dict):
            existing.add(client_key(c))

    added = 0
    for sid in source_ids:
        sset = load_settings(con, settings_col, sid)
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
    save_settings(con, settings_col, target_id, tset)
    return added


def merge_db_for_import(input_db: str, output_db: str, target_id: int, source_ids: List[int]) -> Tuple[str, int]:
    """
    خروجی استاندارد و تک‌فایل برای Import:
    - کپی به work
    - merge
    - wal_checkpoint + journal_mode=DELETE
    - VACUUM INTO output_db  (خروجی تمیز)
    """
    work_db = output_db + ".work"
    shutil.copy2(input_db, work_db)

    con = sqlite3.connect(work_db)
    try:
        if not table_exists(con, "inbounds"):
            raise RuntimeError("جدول inbounds وجود ندارد؛ این فایل x-ui.db معتبر نیست.")

        cur = con.cursor()
        cur.execute("SELECT 1 FROM inbounds WHERE id=? LIMIT 1;", (target_id,))
        if cur.fetchone() is None:
            raise RuntimeError(f"Inbound مقصد با id={target_id} داخل دیتابیس نیست.")

        missing = []
        for sid in source_ids:
            cur.execute("SELECT 1 FROM inbounds WHERE id=? LIMIT 1;", (sid,))
            if cur.fetchone() is None:
                missing.append(sid)
        if missing:
            raise RuntimeError(f"Inboundهای ورودی داخل دیتابیس نیستند: {missing}")

        # merge
        if table_exists(con, "clients"):
            added = merge_clients_table(con, target_id, source_ids)
            mode = "TABLE"
        else:
            added = merge_clients_in_settings(con, target_id, source_ids)
            mode = "JSON"

        con.commit()

        # make import-friendly (fix WAL / file format issues)
        con.execute("PRAGMA wal_checkpoint(FULL);")
        con.execute("PRAGMA journal_mode=DELETE;")
        con.commit()

        # produce clean single-file db
        try:
            con.execute(f"VACUUM INTO '{output_db}';")
            con.commit()
        except sqlite3.OperationalError:
            # اگر VACUUM INTO پشتیبانی نشد: fallback
            # (اکثر سرورها دارند، ولی برای اطمینان)
            con.execute("VACUUM;")
            con.commit()
            shutil.copy2(work_db, output_db)

        return mode, added

    finally:
        con.close()
        try:
            os.remove(work_db)
        except Exception:
            pass


# ------------------------- Handlers -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(START_TEXT, reply_markup=kb_start(), parse_mode="Markdown")


async def start_merge_btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    await q.edit_message_text(
        "📦 لطفاً فایل دیتابیس **x-ui.db** را به صورت Document ارسال کنید.\n\n"
        "نکته: فقط فایل .db بفرستید (زیپ یا عکس نباشد).",
        parse_mode="Markdown",
    )
    return UPLOAD_DB


async def recv_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ لطفاً فایل را به صورت Document ارسال کنید.")
        return UPLOAD_DB

    filename = (doc.file_name or "").lower()
    if not filename.endswith(".db"):
        await update.message.reply_text("❌ فایل باید با پسوند .db باشد.")
        return UPLOAD_DB

    tg_file = await context.bot.get_file(doc.file_id)

    with tempfile.NamedTemporaryFile(prefix="xui_input_", suffix=".db", delete=False) as f:
        local_path = f.name
    await tg_file.download_to_drive(custom_path=local_path)

    context.user_data["db_in"] = local_path

    await update.message.reply_text(
        "✅ دیتابیس دریافت شد.\n\n"
        "🎯 حالا **ID اینباند مقصد (خروجی)** را ارسال کنید:\n"
        "مثال: 12"
    )
    return ASK_TARGET


async def ask_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not is_int_id(text):
        await update.message.reply_text("❌ فقط عدد ارسال کنید (مثلاً 12).")
        return ASK_TARGET

    context.user_data["target_id"] = int(text)

    await update.message.reply_text(
        "🔢 چند تا **Inbound ورودی** دارید؟\n"
        "مثال: 3"
    )
    return ASK_COUNT


async def ask_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not is_int_id(text):
        await update.message.reply_text("❌ فقط عدد ارسال کنید (مثلاً 3).")
        return ASK_COUNT

    n = int(text)
    if not (1 <= n <= 30):
        await update.message.reply_text("❌ تعداد باید بین 1 تا 30 باشد.")
        return ASK_COUNT

    context.user_data["src_count"] = n
    context.user_data["src_ids"] = []

    await update.message.reply_text("✅ عالی. حالا ID ورودی شماره 1 را ارسال کنید:")
    return ASK_SOURCES


async def ask_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not is_int_id(text):
        await update.message.reply_text("❌ فقط عدد ارسال کنید.")
        return ASK_SOURCES

    sid = int(text)
    src_ids: List[int] = context.user_data.get("src_ids", [])
    src_ids.append(sid)
    context.user_data["src_ids"] = src_ids

    n = int(context.user_data["src_count"])
    if len(src_ids) < n:
        await update.message.reply_text(f"✅ ثبت شد. حالا ID ورودی شماره {len(src_ids) + 1} را ارسال کنید:")
        return ASK_SOURCES

    target_id = int(context.user_data["target_id"])
    await update.message.reply_text(
        "🧾 خلاصه درخواست شما:\n\n"
        f"🎯 مقصد: {target_id}\n"
        f"📥 ورودی‌ها: {', '.join(str(x) for x in src_ids)}\n\n"
        "اگر آماده‌ای، بزن روی «انجام بده» ✅",
        reply_markup=kb_confirm(),
    )
    return CONFIRM


async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel_merge":
        db_in = context.user_data.get("db_in")
        try:
            if db_in and os.path.exists(db_in):
                os.remove(db_in)
        except Exception:
            pass
        context.user_data.clear()
        await q.edit_message_text("✅ عملیات لغو شد.\nبرای شروع دوباره /start را بزنید.")
        return ConversationHandler.END

    if q.data != "do_merge":
        return CONFIRM

    db_in = context.user_data.get("db_in")
    target_id = int(context.user_data.get("target_id"))
    src_ids = [int(x) for x in (context.user_data.get("src_ids") or [])]

    if not db_in or not os.path.exists(db_in):
        context.user_data.clear()
        await q.edit_message_text("❌ دیتابیس ورودی پیدا نشد. دوباره از /start شروع کنید.")
        return ConversationHandler.END

    await q.edit_message_text("⏳ در حال ادغام... لطفاً چند ثانیه صبر کنید.")

    out_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="xui_merged_", suffix=".db", delete=False) as f:
            out_path = f.name

        mode, added = merge_db_for_import(db_in, out_path, target_id, src_ids)

        caption = (
            "✅ ادغام انجام شد.\n\n"
            f"🔧 Mode: {mode}\n"
            f"➕ Added clients: {added}\n\n"
            "📦 دیتابیس جدید آماده است (برای Import):"
        )

        await q.message.reply_document(
            document=InputFile(out_path, filename="x-ui.db"),
            caption=caption,
        )

        await q.message.reply_text("برای ادغام جدید /start را بزنید ✅")

    except Exception as e:
        logger.exception("merge failed")
        await q.message.reply_text(
            "❌ ادغام انجام نشد.\n"
            f"خطا: {short_err(e)}\n\n"
            "نکته: مطمئن شو IDها درست هستند و این فایل واقعاً x-ui.db است."
        )
    finally:
        try:
            if db_in and os.path.exists(db_in):
                os.remove(db_in)
        except Exception:
            pass
        try:
            if out_path and os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        context.user_data.clear()

    return ConversationHandler.END


# ------------------------- Global error handler -------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
    try:
        if isinstance(update, Update):
            if update.message:
                await update.message.reply_text("⚠️ یک خطای داخلی رخ داد. لطفاً دوباره تلاش کنید.")
            elif update.callback_query:
                await update.callback_query.message.reply_text("⚠️ یک خطای داخلی رخ داد. لطفاً دوباره تلاش کنید.")
    except Exception:
        pass


# ------------------------- Main -------------------------
def main():
    token = get_token()
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_merge_btn, pattern="^start_merge_db$")],
        states={
            UPLOAD_DB: [MessageHandler(filters.Document.ALL, recv_db)],
            ASK_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_target)],
            ASK_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_count)],
            ASK_SOURCES: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_sources)],
            CONFIRM: [CallbackQueryHandler(confirm_cb, pattern="^(do_merge|cancel_merge)$")],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_error_handler(on_error)

    app.run_polling()


if __name__ == "__main__":
    main()
