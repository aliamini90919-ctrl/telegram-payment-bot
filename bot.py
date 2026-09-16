import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest


# =========================================================
# CONFIG
# =========================================================
https://github.com/aliamini90919-ctrl/telegram-payment-bot/security
# =========================================================
# تنظیمات اصلی — بدون نیاز به فایل .env
# =========================================================
# توکن همین ربات را اینجا قرار بده.
# شناسه عددی ادمین‌های اصلی
ADMIN_IDS = {
    8947012753,
    418908614,
}

# رمز /adpass برای تبدیل کاربر عادی به ادمین
ADPASS_PASSWORD = "@dm1nP@33w0rd"

# ادمین‌هایی که از طریق /adpass ارتقا پیدا کرده‌اند؛ در دیتابیس هم ذخیره می‌شوند.
PROMOTED_ADMIN_IDS = set()

# فایل دیتابیس این ربات
DB_PATH = "payments.db"

# تنها کاربری که نقش A Content دارد
A_CONTENT_ID = 7118132097

# اعداد ورودی کاربر/سقف حساب بر حسب «میلیون تومان» هستند.
AMOUNT_MULTIPLIER = 1_000_000

REMINDER_INTERVAL_SECONDS = 3600
REMINDER_AFTER_HOURS = 24

# شناسه آخرین پیام تعاملی هر کاربر؛ برای بی‌اثر کردن دکمه‌های پیام‌های قدیمی
LATEST_MESSAGE_IDS = {}


async def tracked_reply(update, context, text, **kwargs):
    # اگر پیام دکمه مشخصی ندارد، یک مسیر برگشت به منوی اصلی اضافه می‌کنیم.
    kwargs.setdefault("reply_markup", back_button())
    message = await update.message.reply_text(text, **kwargs)
    user = update.effective_user
    if user and message:
        LATEST_MESSAGE_IDS[user.id] = message.message_id
    return message


async def tracked_send_message(bot, chat_id, *args, **kwargs):
    message = await bot.send_message(chat_id, *args, **kwargs)
    LATEST_MESSAGE_IDS[chat_id] = message.message_id
    return message


async def tracked_send_photo(bot, chat_id, *args, **kwargs):
    message = await bot.send_photo(chat_id, *args, **kwargs)
    LATEST_MESSAGE_IDS[chat_id] = message.message_id
    return message


async def tracked_send_document(bot, chat_id, *args, **kwargs):
    message = await bot.send_document(chat_id, *args, **kwargs)
    LATEST_MESSAGE_IDS[chat_id] = message.message_id
    return message


async def tracked_edit(query, context, text, **kwargs):
    # اگر کیبورد مشخص نشده، حداقل دکمه بازگشت به منوی اصلی را نگه می‌داریم.
    kwargs.setdefault("reply_markup", back_button())
    message = await query.edit_message_text(text, **kwargs)
    user = query.from_user
    if user and query.message:
        LATEST_MESSAGE_IDS[user.id] = query.message.message_id
    elif user and message:
        LATEST_MESSAGE_IDS[user.id] = message.message_id
    return message


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================================================
# TELEGRAM CONNECTION - HIGH PERFORMANCE
# =========================================================

BOT_REQUEST = HTTPXRequest(
    connection_pool_size=100,
    pool_timeout=5.0,
    connect_timeout=5.0,
    read_timeout=30.0,
    write_timeout=30.0,
)

GET_UPDATES_REQUEST = HTTPXRequest(
    connection_pool_size=100,
    pool_timeout=5.0,
    connect_timeout=5.0,
    read_timeout=35.0,
    write_timeout=30.0,
)


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA temp_store=MEMORY")

    return conn


def init_db():
    conn = db()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS payment_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_name TEXT NOT NULL,
        account_number TEXT NOT NULL,
        capacity INTEGER NOT NULL DEFAULT 0,
        reserved_amount INTEGER NOT NULL DEFAULT 0,
        paid_amount INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        deadline_at TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        reservation_name TEXT,
        account_number_snapshot TEXT,
        amount INTEGER NOT NULL,
        target_id INTEGER,
        status TEXT NOT NULL DEFAULT 'waiting',
        receipt_file_id TEXT,
        receipt_type TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        next_reminder_at TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_requests_user
        ON requests(user_id);

    CREATE INDEX IF NOT EXISTS idx_requests_status
        ON requests(status);

    CREATE INDEX IF NOT EXISTS idx_requests_target
        ON requests(target_id);

    CREATE INDEX IF NOT EXISTS idx_targets_active
        ON payment_targets(active);

    CREATE TABLE IF NOT EXISTS promoted_admins (
        user_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL
    );
    """)

    # ادمین‌های ایجادشده با /adpass را بعد از هر راه‌اندازی بارگذاری کن.
    PROMOTED_ADMIN_IDS.clear()
    for row in conn.execute("SELECT user_id FROM promoted_admins").fetchall():
        PROMOTED_ADMIN_IDS.add(int(row["user_id"]))

    # -----------------------------------------------------
    # Migration
    # -----------------------------------------------------

    request_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(requests)"
        ).fetchall()
    }

    migrations = {
        "username":
            "ALTER TABLE requests ADD COLUMN username TEXT",

        "first_name":
            "ALTER TABLE requests ADD COLUMN first_name TEXT",

        "reservation_name":
            "ALTER TABLE requests ADD COLUMN reservation_name TEXT",

        "account_number_snapshot":
            "ALTER TABLE requests ADD COLUMN account_number_snapshot TEXT",

        "target_id":
            "ALTER TABLE requests ADD COLUMN target_id INTEGER",

        "status":
            "ALTER TABLE requests ADD COLUMN status TEXT DEFAULT 'waiting'",

        "receipt_file_id":
            "ALTER TABLE requests ADD COLUMN receipt_file_id TEXT",

        "receipt_type":
            "ALTER TABLE requests ADD COLUMN receipt_type TEXT",

        "created_at":
            "ALTER TABLE requests ADD COLUMN created_at TEXT",

        "updated_at":
            "ALTER TABLE requests ADD COLUMN updated_at TEXT",

        "next_reminder_at":
            "ALTER TABLE requests ADD COLUMN next_reminder_at TEXT",
    }

    for column, sql in migrations.items():
        if column not in request_columns:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass

    target_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(payment_targets)"
        ).fetchall()
    }

    target_migrations = {
        "reserved_amount":
            "ALTER TABLE payment_targets ADD COLUMN reserved_amount INTEGER DEFAULT 0",

        "paid_amount":
            "ALTER TABLE payment_targets ADD COLUMN paid_amount INTEGER DEFAULT 0",

        "active":
            "ALTER TABLE payment_targets ADD COLUMN active INTEGER DEFAULT 1",

        "created_at":
            "ALTER TABLE payment_targets ADD COLUMN created_at TEXT",
        "deadline_at":
            "ALTER TABLE payment_targets ADD COLUMN deadline_at TEXT",
    }

    for column, sql in target_migrations.items():
        if column not in target_columns:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass

    # برای درخواست‌های قدیمی، اگر شماره حساب زمان رزرو ذخیره نشده
    # باشد، از شماره فعلی حساب به عنوان نزدیک‌ترین مقدار ممکن استفاده می‌کنیم.
    try:
        conn.execute("""
            UPDATE requests
            SET account_number_snapshot = (
                SELECT account_number
                FROM payment_targets
                WHERE payment_targets.id = requests.target_id
            )
            WHERE target_id IS NOT NULL
              AND (account_number_snapshot IS NULL OR account_number_snapshot = '')
        """)
    except sqlite3.OperationalError:
        pass

    # درخواست‌های قدیمی که حساب گرفته‌اند ولی هنوز فیش ندارند،
    # از زمان اجرای فعلی ۲۴ ساعت بعد اولین یادآوری را می‌گیرند.
    try:
        old_pending = conn.execute("""
            SELECT id
            FROM requests
            WHERE status = 'reserved'
              AND receipt_file_id IS NULL
              AND next_reminder_at IS NULL
        """).fetchall()
        for old_row in old_pending:
            conn.execute("""
                UPDATE requests
                SET next_reminder_at = ?
                WHERE id = ?
            """, (reminder_due(), old_row['id']))
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


# =========================================================
# HELPERS
# =========================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_amount(value):
    """Parse a user/admin amount in million Toman and return whole Toman.

    Examples: 11 -> 11,000,000 ; 11.5 -> 11,500,000
    Supports Persian/Arabic digits and decimal separator (٫).
    """
    value = str(value).strip()

    # Persian/Arabic-Indic digits -> ASCII digits
    translation = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )
    value = value.translate(translation)
    value = value.replace("٫", ".")
    value = value.replace("٬", "")
    value = value.replace(" ", "")

    # A comma is treated as a thousands separator, matching the old behavior.
    value = value.replace(",", "")

    try:
        amount_million = Decimal(value)

        if amount_million <= 0:
            return None

        # Convert million-Toman input to whole Toman. Decimal avoids float errors.
        amount_toman = amount_million * Decimal(AMOUNT_MULTIPLIER)

        if amount_toman != amount_toman.to_integral_value():
            return None

        return int(amount_toman)

    except (InvalidOperation, ValueError, TypeError):
        return None


def fmt_amount(amount):
    return f"{int(amount):,}"


def is_admin(user_id):
    return user_id in ADMIN_IDS or user_id in PROMOTED_ADMIN_IDS


def promote_to_admin(user_id):
    """ثبت دائمی کاربر به‌عنوان ادمین."""
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO promoted_admins (user_id, created_at) VALUES (?, ?)",
            (user_id, now),
        )
        conn.commit()
    finally:
        conn.close()
    PROMOTED_ADMIN_IDS.add(user_id)


def is_a_content(user_id):
    return user_id == A_CONTENT_ID and not is_admin(user_id)


def is_staff(user_id):
    return is_admin(user_id) or is_a_content(user_id)


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def parse_deadline(value):
    value = (value or "").strip()
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Tehran"))
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def deadline_display(value):
    dt = parse_iso(value)
    if not dt:
        return "بدون ددلاین"
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Asia/Tehran")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M UTC")


def reminder_due(created_at=None):
    base = datetime.now(timezone.utc)
    if created_at:
        parsed = parse_iso(created_at)
        if parsed:
            base = parsed
    return (base + timedelta(hours=REMINDER_AFTER_HOURS)).isoformat()


def next_reminder_after_now():
    return (datetime.now(timezone.utc) + timedelta(hours=REMINDER_AFTER_HOURS)).isoformat()


def display_amount(value):
    return int(value) * AMOUNT_MULTIPLIER


def available_amount(target):
    return max(
        0,
        int(target["capacity"])
        - int(target["reserved_amount"])
        - int(target["paid_amount"]),
    )


def clear_state(context):
    context.user_data.clear()


# =========================================================
# KEYBOARDS
# =========================================================

def user_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 دریافت حساب",
                callback_data="u_get",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "📋 درخواست‌های من",
                callback_data="u_requests",
             style="primary"),
            InlineKeyboardButton(
                "🧾 ارسال فیش",
                callback_data="u_receipt",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ راهنما",
                callback_data="u_help",
             style="primary"),
        ],
    ])


def a_content_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 دریافت حساب",
                callback_data="u_get",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "📋 درخواست‌های من",
                callback_data="u_requests",
             style="primary"),
            InlineKeyboardButton(
                "🧾 ارسال فیش",
                callback_data="u_receipt",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "🧾 فیش‌های دریافتی",
                callback_data="c_receipts",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ راهنما",
                callback_data="u_help",
             style="primary"),
        ],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ افزودن حساب",
                callback_data="a_add",
             style="success"),
        ],
        [
            InlineKeyboardButton(
                "📊 وضعیت حساب‌ها",
                callback_data="a_status",
             style="primary"),
            InlineKeyboardButton(
                "⏳ در انتظار واریز",
                callback_data="a_payment_pending",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "🧾 فیش‌های در انتظار تایید",
                callback_data="a_pending",
             style="success"),
        ],
        [
            InlineKeyboardButton(
                "📜 تاریخچه",
                callback_data="a_history",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "👤 پنل کاربر",
                callback_data="u_panel",
             style="primary"),
        ],
    ])


def back_button():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="main",
             style="primary"),
        ],
    ])


# =========================================================
# USER REQUEST STATUS MENU
# =========================================================

def requests_status_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔴 رد شده ها",
                callback_data="ur_rejected",
             style="danger"),
            InlineKeyboardButton(
                "🟢 تایید شده ها",
                callback_data="ur_paid",
             style="success"),
        ],
        [
            InlineKeyboardButton(
                "⚫ لغو شده ها",
                callback_data="ur_cancelled",
             style="danger"),
        ],
        [
            InlineKeyboardButton(
                "⏳ در انتظار حساب",
                callback_data="ur_waiting",
             style="primary"),
            InlineKeyboardButton(
                "💳 در انتظار واریز",
                callback_data="ur_payment_pending",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "🧾 در انتظار تایید فیش",
                callback_data="ur_reserved",
             style="success"),
        ],
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="main",
             style="primary"),
        ],
    ])


def requests_status_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔴 رد شده ها",
                callback_data="ur_rejected",
             style="danger"),
            InlineKeyboardButton(
                "🟢 تایید شده ها",
                callback_data="ur_paid",
             style="success"),
        ],
        [
            InlineKeyboardButton(
                "⚫ لغو شده ها",
                callback_data="ur_cancelled",
             style="danger"),
        ],
        [
            InlineKeyboardButton(
                "⏳ در انتظار حساب",
                callback_data="ur_waiting",
             style="primary"),
            InlineKeyboardButton(
                "💳 در انتظار واریز",
                callback_data="ur_payment_pending",
             style="primary"),
        ],
        [
            InlineKeyboardButton(
                "🧾 در انتظار تایید فیش",
                callback_data="ur_reserved",
             style="success"),
        ],
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="u_requests",
             style="primary"),
        ],
    ])


# =========================================================
# MAIN MENU
# =========================================================

def menu_text(user):
    if is_admin(user.id):
        return (
            "🤖 *ربات مدیریت پرداخت*\n\n"
            "سلام 👋\n"
            "گزینه موردنظر را انتخاب کنید.\n\n"
            "🔐 دسترسی مدیریت فعال است."
        )

    if is_a_content(user.id):
        return (
            "🤖 *ربات مدیریت پرداخت*\n\n"
            "سلام 👋\n"
            "گزینه موردنظر را انتخاب کنید.\n\n"
            "🧾 دسترسی A Content فعال است."
        )

    return (
        "🤖 *ربات مدیریت پرداخت*\n\n"
        "سلام 👋\n"
        "گزینه موردنظر را انتخاب کنید."
    )


async def show_main_menu(
    update,
    context,
    edit=False,
):
    clear_state(context)

    user = update.effective_user

    if is_admin(user.id):
        keyboard = admin_menu()
    elif is_a_content(user.id):
        keyboard = a_content_menu()
    else:
        keyboard = user_menu()

    text = menu_text(user)

    if edit:
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    else:
        await tracked_reply(update, context, 
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )


# =========================================================
# /ADPASS - PROMOTE USER TO ADMIN
# =========================================================

async def command_adpass(update, context):
    user = update.effective_user

    # این مسیر فقط برای کاربر عادی است و هیچ دکمه‌ای برای آن وجود ندارد.
    if is_staff(user.id):
        await tracked_reply(update, context, 
            "ℹ️ شما در حال حاضر دسترسی مدیریتی دارید."
        )
        return

    if not ADPASS_PASSWORD:
        logger.error("ADPASS_PASSWORD is not configured")
        await tracked_reply(update, context, 
            "❌ قابلیت ارتقای ادمین در حال حاضر تنظیم نشده است."
        )
        return

    clear_state(context)
    context.user_data["adpass_password"] = True

    await tracked_reply(update, context, 
        "🔐 لطفاً رمز عبور مدیریت را وارد کنید:\n\n"
        "⚠️ رمز را فقط در همین گفت‌وگو ارسال کنید."
    )


async def receive_adpass_password(update, context):
    if not context.user_data.get("adpass_password"):
        return False

    password = (update.message.text or "").strip()
    context.user_data.pop("adpass_password", None)

    if not ADPASS_PASSWORD or password != ADPASS_PASSWORD:
        await tracked_reply(update, context, 
            "❌ رمز عبور اشتباه است. دسترسی مدیریت فعال نشد."
        )
        return True

    user_id = update.effective_user.id
    promote_to_admin(user_id)

    await tracked_reply(update, context, 
        "✅ رمز صحیح است.\n\n"
        "🔐 دسترسی ادمین برای حساب شما فعال شد.\n"
        "از این به بعد منوی مدیریت را مشاهده خواهید کرد."
    )

    # منوی مدیریت را بلافاصله نمایش بده.
    await show_main_menu(update, context, edit=False)
    return True


# =========================================================
# START
# =========================================================

async def start(update, context):
    await show_main_menu(
        update,
        context,
        edit=False,
    )


# =========================================================
# GET ACCOUNT
# =========================================================

async def get_account_menu(update, context):
    query = update.callback_query

    await query.answer()

    clear_state(context)

    context.user_data["amount"] = True

    await tracked_edit(query, context, 
        "💰 *دریافت حساب*\n\n"
        "مبلغ موردنظر را به تومان وارد کنید.\n\n"
        "مثال:\n"
        "`11.5`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


async def receive_amount(update, context):
    if not context.user_data.get("amount"):
        return

    amount = parse_amount(
        update.message.text
    )

    if amount is None:
        await tracked_reply(update, context, 
            "❌ مبلغ نامعتبر است.\n\n"
            "مثال:\n"
            "`11.5`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    context.user_data.pop(
        "amount",
        None,
    )

    # بعد از مبلغ، نام صاحب رزرو را می‌گیریم.
    context.user_data["pending_amount"] = amount
    context.user_data["reservation_name"] = True

    await tracked_reply(update, context, 
        "👤 لطفاً *نام و نام خانوادگی* خود را برای ثبت رزرو وارد کنید:\n\n"
        "مثال: `علی رضایی`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


async def receive_reservation_name(update, context):
    if not context.user_data.get("reservation_name"):
        return

    name = (update.message.text or "").strip()
    if len(name) < 2 or len(name) > 100:
        await tracked_reply(update, context, 
            "❌ نام واردشده معتبر نیست. لطفاً نام و نام خانوادگی را وارد کنید."
        )
        return

    amount = context.user_data.pop("pending_amount", None)
    context.user_data.pop("reservation_name", None)

    if amount is None:
        await tracked_reply(update, context, 
            "❌ اطلاعات مبلغ پیدا نشد. لطفاً دوباره از گزینه «دریافت حساب» شروع کنید.",
            reply_markup=user_menu(),
        )
        return

    await create_request(
        update,
        context,
        amount,
        reservation_name=name,
    )


async def create_request(
    update,
    context,
    amount,
    reservation_name=None,
):
    user = update.effective_user

    conn = db()
    conn.execute("BEGIN IMMEDIATE")

    targets = conn.execute("""
        SELECT *
        FROM payment_targets
        WHERE active = 1
        AND (deadline_at IS NULL OR deadline_at > ?)
        AND (capacity - reserved_amount - paid_amount) >= ?
        ORDER BY CASE WHEN deadline_at IS NULL THEN 1 ELSE 0 END ASC, deadline_at ASC, id ASC
        LIMIT 1
    """, (
        now_iso(),
        amount,
    )).fetchone()

    created = now_iso()

    # -----------------------------------------------------
    # ACCOUNT FOUND
    # -----------------------------------------------------

    if targets:

        cursor = conn.execute("""
            INSERT INTO requests (
                user_id,
                username,
                first_name,
                reservation_name,
                account_number_snapshot,
                amount,
                target_id,
                status,
                created_at,
                updated_at,
                next_reminder_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?
            )
        """, (
            user.id,
            user.username,
            user.first_name,
            reservation_name,
            targets["account_number"],
            amount,
            targets["id"],
            created,
            created,
            reminder_due(created),
        ))

        request_id = cursor.lastrowid

        conn.execute("""
            UPDATE payment_targets
            SET reserved_amount =
                reserved_amount + ?
            WHERE id = ?
        """, (
            amount,
            targets["id"],
        ))

        conn.commit()

        remaining = (
            int(targets["capacity"])
            - int(targets["reserved_amount"])
            - int(targets["paid_amount"])
            - amount
        )

        conn.close()

        await tracked_reply(update, context, 
            "✅ *حساب برای شما اختصاص داده شد*\n\n"
            f"🆔 درخواست: `{request_id}`\n\n"
            f"👤 صاحب حساب:\n"
            f"`{targets['owner_name']}`\n\n"
            f"🏦 شماره حساب:\n"
            f"`{targets['account_number']}`\n\n"
            f"💰 مبلغ:\n"
            f"`{fmt_amount(amount)}` تومان\n\n"
            f"📊 باقی‌مانده:\n"
            f"`{fmt_amount(max(0, remaining))}` تومان\n\n"
            "پس از پرداخت، فیش را ارسال کنید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🧾 ارسال فیش",
                        callback_data=f"r_{request_id}",
                     style="primary"),
                ],
                [
                    InlineKeyboardButton(
                        "📋 درخواست‌های من",
                        callback_data="u_requests",
                     style="primary"),
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ منوی اصلی",
                        callback_data="main",
                     style="primary"),
                ],
            ]),
        )

        return

    # -----------------------------------------------------
    # WAITING
    # -----------------------------------------------------

    cursor = conn.execute("""
        INSERT INTO requests (
            user_id,
            username,
            first_name,
            reservation_name,
            account_number_snapshot,
            amount,
            status,
            created_at,
            updated_at,
            next_reminder_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, 'waiting', ?, ?, NULL
        )
    """, (
        user.id,
        user.username,
        user.first_name,
        reservation_name,
        None,
        amount,
        created,
        created,
    ))

    request_id = cursor.lastrowid

    max_available = conn.execute("""
        SELECT COALESCE(
            MAX(
                capacity
                - reserved_amount
                - paid_amount
            ),
            0
        )
        AS max_available
        FROM payment_targets
        WHERE active = 1
    """).fetchone()["max_available"]

    conn.commit()
    conn.close()

    await tracked_reply(update, context, 
        "⏳ *درخواست شما در صف انتظار قرار گرفت*\n\n"
        f"🆔 درخواست: `{request_id}`\n"
        f"💰 مبلغ: `{fmt_amount(amount)}` تومان\n\n"
        "در حال حاضر حساب مناسبی وجود ندارد.\n\n"
        f"📊 بیشترین ظرفیت فعلی:\n"
        f"`{fmt_amount(max_available)}` تومان\n\n"
        "🔔 با اضافه شدن حساب مناسب، "
        "درخواست شما خودکار اختصاص داده می‌شود.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )

    # اطلاع ادمین‌ها
    admin_text = (
        "🚨 *درخواست جدید*\n\n"
        f"🆔 `{request_id}`\n"
        f"👤 نام رزرو: {reservation_name or '-'}\n"
        f"🔹 @{user.username or '-'}\n"
        f"🔢 `{user.id}`\n"
        f"💰 `{fmt_amount(amount)}` تومان\n\n"
        "⚠️ حساب مناسب موجود نیست."
    )

    for admin_id in ADMIN_IDS:
        try:
            await tracked_send_message(context.bot, 
                admin_id,
                admin_text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


# =========================================================
# MY REQUESTS
# =========================================================

async def my_requests_menu(update, context):
    query = update.callback_query

    await query.answer()

    clear_state(context)

    await tracked_edit(query, context, 
        "📋 *درخواست‌های من*\n\n"
        "لطفاً وضعیت درخواست‌ها را انتخاب کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=requests_status_menu(),
    )


async def my_requests_by_status(
    update,
    context,
    status,
):
    query = update.callback_query

    await query.answer()

    user_id = update.effective_user.id

    status_names = {
        "waiting": "⏳ در انتظار حساب",
        "reserved": "🧾 در انتظار تایید فیش",
        "payment_pending": "💳 در انتظار واریز",
        "paid": "🟢 تایید شده ها",
        "rejected": "🔴 رد شده ها",
        "cancelled": "⚫ لغو شده ها",
    }

    conn = db()

    if status == "payment_pending":
        rows = conn.execute("""
            SELECT id, amount, status, receipt_file_id, created_at
            FROM requests
            WHERE user_id = ?
              AND status = 'reserved'
              AND receipt_file_id IS NULL
            ORDER BY id DESC
            LIMIT 20
        """, (user_id,)).fetchall()
    elif status == "reserved":
        rows = conn.execute("""
            SELECT id, amount, status, receipt_file_id, created_at
            FROM requests
            WHERE user_id = ?
              AND status = 'reserved'
              AND receipt_file_id IS NOT NULL
            ORDER BY id DESC
            LIMIT 20
        """, (user_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, amount, status, receipt_file_id, created_at
            FROM requests
            WHERE user_id = ?
              AND status = ?
            ORDER BY id DESC
            LIMIT 20
        """, (user_id, status)).fetchall()

    conn.close()

    title = status_names.get(
        status,
        "درخواست‌ها",
    )

    # -----------------------------------------------------
    # NO REQUEST
    # -----------------------------------------------------

    if not rows:

        text = (
            f"📋 *{title}*\n\n"
            "درخواستی در این بخش وجود ندارد."
        )

    # -----------------------------------------------------
    # REQUESTS
    # -----------------------------------------------------

    else:

        parts = [
            f"📋 *{title}*\n"
        ]

        for row in rows:

            receipt = (
                "🧾 فیش دریافت شده"
                if row["receipt_file_id"]
                else "📎 بدون فیش"
            )

            parts.append(
                f"🆔 درخواست: `{row['id']}`\n"
                f"💰 مبلغ: `{fmt_amount(row['amount'])}` تومان\n"
                f"📌 وضعیت: {status_names.get(row['status'], row['status'])}\n"
                f"{receipt}\n"
                "━━━━━━━━━━━━━━"
            )

        text = "\n".join(parts)

    await tracked_edit(query, context, 
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=requests_status_keyboard(),
    )


# =========================================================
# RECEIPT
# =========================================================

async def receipt_menu(update, context):
    query = update.callback_query

    await query.answer()
    clear_state(context)

    user_id = query.from_user.id
    conn = db()
    rows = conn.execute("""
        SELECT id, reservation_name, first_name, amount, created_at
        FROM requests
        WHERE user_id = ?
          AND status = 'reserved'
          AND receipt_file_id IS NULL
        ORDER BY id DESC
        LIMIT 50
    """, (user_id,)).fetchall()
    conn.close()

    if not rows:
        await tracked_edit(query, context, 
            "🧾 *ارسال فیش*\n\n"
            "❌ هیچ رزروی که هنوز فیش آن ارسال نشده باشد پیدا نشد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_button(),
        )
        return

    keyboard = []
    for row in rows:
        name = row["reservation_name"] or row["first_name"] or "بدون نام"
        keyboard.append([
            InlineKeyboardButton(
                f"🧾 #{row['id']} | {name} | {fmt_amount(row['amount'])} تومان",
                callback_data=f"receipt_select_{row['id']}",
             style="primary")
        ])

    keyboard.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="main", style="primary")])

    await tracked_edit(query, context, 
        "🧾 *ارسال فیش*\n\n"
        "یکی از رزروهای بدون فیش را انتخاب کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def receipt_select(update, context, request_id):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    conn = db()
    request = conn.execute("""
        SELECT id, status, receipt_file_id, amount, reservation_name, first_name
        FROM requests
        WHERE id = ?
          AND user_id = ?
    """, (request_id, user_id)).fetchone()
    conn.close()

    if not request:
        await tracked_edit(query, context, 
            "❌ این رزرو پیدا نشد.",
            reply_markup=back_button(),
        )
        return

    if request["status"] != "reserved" or request["receipt_file_id"]:
        await tracked_edit(query, context, 
            "❌ این رزرو دیگر برای ارسال فیش قابل انتخاب نیست.",
            reply_markup=back_button(),
        )
        return

    context.user_data["receipt_request"] = request_id

    name = request["reservation_name"] or request["first_name"] or "-"
    await tracked_edit(query, context, 
        "📎 *ارسال فیش*\n\n"
        f"🆔 درخواست: `{request_id}`\n"
        f"👤 نام رزرو: {name}\n"
        f"💰 مبلغ: `{fmt_amount(request['amount'])}` تومان\n\n"
        "حالا فیش پرداخت را به صورت عکس یا فایل ارسال کنید.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


async def receive_receipt(
    update,
    context,
):
    request_id = context.user_data.get(
        "receipt_request"
    )

    if not request_id:
        return

    user = update.effective_user

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        receipt_type = "photo"

    elif update.message.document:
        file_id = update.message.document.file_id
        receipt_type = "document"

    else:
        return

    conn = db()

    request = conn.execute("""
        SELECT *
        FROM requests
        WHERE id = ?
        AND user_id = ?
    """, (
        request_id,
        user.id,
    )).fetchone()

    if not request:
        conn.close()
        return

    if request["status"] != "reserved" or request["receipt_file_id"]:
        conn.close()
        context.user_data.pop("receipt_request", None)
        await tracked_reply(update, context, 
            "❌ این درخواست دیگر آماده دریافت فیش نیست."
        )
        return

    conn.execute("""
        UPDATE requests
        SET receipt_file_id = ?,
            receipt_type = ?,
            updated_at = ?,
            next_reminder_at = NULL
        WHERE id = ?
    """, (
        file_id,
        receipt_type,
        now_iso(),
        request_id,
    ))

    conn.commit()
    conn.close()

    context.user_data.pop(
        "receipt_request",
        None,
    )

    await tracked_reply(update, context, 
        "✅ *فیش دریافت شد.*\n\n"
        f"🆔 درخواست: `{request_id}`\n\n"
        "⏳ برای بررسی ارسال شد.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=user_menu(),
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تأیید",
                callback_data=f"ok_{request_id}",
             style="success"),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"no_{request_id}",
             style="danger"),
        ],
    ])

    admin_text = (
        "🧾 *فیش جدید*\n\n"
        f"🆔 درخواست: `{request_id}`\n"
        f"👤 نام رزرو: {request['reservation_name'] or request['first_name'] or '-'}\n"
        f"🔹 @{user.username or '-'}\n"
        f"💰 `{fmt_amount(request['amount'])}` تومان"
    )

    for admin_id in ADMIN_IDS:
        try:
            await tracked_send_message(context.bot, 
                admin_id,
                admin_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )

            if receipt_type == "photo":
                await tracked_send_photo(context.bot, 
                    admin_id,
                    file_id,
                    caption=f"🧾 فیش #{request_id}",
                )
            else:
                await tracked_send_document(context.bot, 
                    admin_id,
                    file_id,
                    caption=f"🧾 فیش #{request_id}",
                )

        except Exception:
            pass

    # A Content هم همان فیش را دریافت می‌کند.
    if not is_admin(A_CONTENT_ID):
        try:
            await tracked_send_message(context.bot, 
                A_CONTENT_ID,
                admin_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
            if receipt_type == "photo":
                await tracked_send_photo(context.bot, 
                    A_CONTENT_ID,
                    file_id,
                    caption=f"🧾 فیش #{request_id}",
                )
            else:
                await tracked_send_document(context.bot, 
                    A_CONTENT_ID,
                    file_id,
                    caption=f"🧾 فیش #{request_id}",
                )
        except Exception:
            pass


# =========================================================
# A CONTENT - RECEIPTS
# =========================================================

async def a_content_receipts(update, context):
    query = update.callback_query
    await query.answer()

    if not is_a_content(query.from_user.id):
        return

    conn = db()
    rows = conn.execute("""
        SELECT id, amount, user_id, username, first_name
        FROM requests
        WHERE status = 'reserved'
          AND receipt_file_id IS NOT NULL
        ORDER BY id ASC
        LIMIT 50
    """).fetchall()
    conn.close()

    if not rows:
        await tracked_edit(query, context, 
            "🧾 *فیش‌های دریافتی*\n\n"
            "فعلاً فیشی برای بررسی وجود ندارد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_button(),
        )
        return

    keyboard = []
    for row in rows:
        keyboard.append([
            InlineKeyboardButton(
                f"🧾 #{row['id']} | {fmt_amount(row['amount'])}",
                callback_data=f"c_receipt_{row['id']}",
             style="primary")
        ])
    keyboard.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="main", style="primary")])

    await tracked_edit(query, context, 
        "🧾 *فیش‌های دریافتی*\n\nیک فیش را برای بررسی انتخاب کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def a_content_receipt_item(update, context, request_id):
    query = update.callback_query
    await query.answer()

    if not is_a_content(query.from_user.id):
        return

    conn = db()
    row = conn.execute("""
        SELECT * FROM requests
        WHERE id = ?
          AND status = 'reserved'
          AND receipt_file_id IS NOT NULL
    """, (request_id,)).fetchone()
    conn.close()

    if not row:
        await tracked_edit(query, context, 
            "❌ این فیش پیدا نشد یا قبلاً بررسی شده است.",
            reply_markup=back_button(),
        )
        return

    text = (
        "🧾 *بررسی فیش*\n\n"
        f"🆔 `{row['id']}`\n"
        f"👤 نام رزرو: {row['reservation_name'] or row['first_name'] or '-'}\n"
        f"🔹 @{row['username'] or '-'}\n"
        f"🔢 `{row['user_id']}`\n"
        f"💰 `{fmt_amount(row['amount'])}` تومان"
    )

    await tracked_edit(query, context, 
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تأیید", callback_data=f"ok_{request_id}", style="success"),
                InlineKeyboardButton("❌ رد", callback_data=f"no_{request_id}", style="danger"),
            ],
            [InlineKeyboardButton("⬅️ برگشت", callback_data="c_receipts", style="primary")],
        ]),
    )

    try:
        if row['receipt_type'] == 'photo':
            await tracked_send_photo(context.bot, 
                query.from_user.id,
                row['receipt_file_id'],
                caption=f"🧾 فیش درخواست #{request_id}",
            )
        else:
            await tracked_send_document(context.bot, 
                query.from_user.id,
                row['receipt_file_id'],
                caption=f"🧾 فیش درخواست #{request_id}",
            )
    except Exception:
        pass


# =========================================================
# APPROVE
# =========================================================

async def approve(update, context, request_id):
    query = update.callback_query

    await query.answer("✅ تأیید شد")

    if not is_staff(query.from_user.id):
        return

    conn = db()

    request = conn.execute("""
        SELECT *
        FROM requests
        WHERE id = ?
          AND status = 'reserved'
          AND receipt_file_id IS NOT NULL
    """, (
        request_id,
    )).fetchone()

    if not request:
        conn.close()
        return

    if request["status"] != "reserved":
        conn.close()
        return

    conn.execute("""
        UPDATE requests
        SET status = 'paid',
            updated_at = ?,
            next_reminder_at = NULL
        WHERE id = ?
          AND status = 'reserved'
          AND receipt_file_id IS NOT NULL
    """, (
        now_iso(),
        request_id,
    ))

    if request["target_id"]:
        conn.execute("""
            UPDATE payment_targets
            SET reserved_amount =
                MAX(
                    0,
                    reserved_amount - ?
                ),
                paid_amount =
                paid_amount + ?
            WHERE id = ?
        """, (
            request["amount"],
            request["amount"],
            request["target_id"],
        ))

    conn.commit()
    conn.close()

    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    try:
        await tracked_send_message(context.bot, 
            request["user_id"],
            "✅ *پرداخت تأیید شد*\n\n"
            f"🆔 `{request_id}`\n"
            f"💰 `{fmt_amount(request['amount'])}` تومان\n\n"
            "پرداخت شما با موفقیت ثبت شد. 🎉",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=user_menu(),
        )
    except Exception:
        pass


# =========================================================
# REJECT
# =========================================================

async def reject(update, context, request_id):
    query = update.callback_query

    await query.answer("❌ رد شد")

    if not is_staff(query.from_user.id):
        return

    conn = db()

    request = conn.execute("""
        SELECT *
        FROM requests
        WHERE id = ?
          AND status = 'reserved'
          AND receipt_file_id IS NOT NULL
    """, (
        request_id,
    )).fetchone()

    if not request:
        conn.close()
        return

    if request["status"] != "reserved":
        conn.close()
        return

    conn.execute("""
        UPDATE requests
        SET status = 'rejected',
            updated_at = ?,
            next_reminder_at = NULL
        WHERE id = ?
    """, (
        now_iso(),
        request_id,
    ))

    if request["target_id"]:
        conn.execute("""
            UPDATE payment_targets
            SET reserved_amount =
                MAX(
                    0,
                    reserved_amount - ?
                )
            WHERE id = ?
        """, (
            request["amount"],
            request["target_id"],
        ))

    conn.commit()
    conn.close()

    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    try:
        await tracked_send_message(context.bot, 
            request["user_id"],
            "❌ *پرداخت رد شد*\n\n"
            f"🆔 `{request_id}`\n"
            f"💰 `{fmt_amount(request['amount'])}` تومان",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=user_menu(),
        )
    except Exception:
        pass


# =========================================================
# ADMIN STATUS
# =========================================================

async def admin_status(update, context):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()
    rows = conn.execute("""
        SELECT
            id,
            owner_name,
            account_number,
            capacity,
            reserved_amount,
            paid_amount,
            active,
            deadline_at
        FROM payment_targets
        ORDER BY id ASC
    """).fetchall()
    conn.close()

    if not rows:
        await tracked_edit(query, context, 
            "📊 *وضعیت حساب‌ها*\n\nهیچ حسابی وجود ندارد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_button(),
        )
        return

    parts = ["📊 *وضعیت حساب‌ها*\n"]
    keyboard = []

    for row in rows:
        remaining = available_amount(row)
        status = "🟢 فعال" if row["active"] else "🔴 حذف/غیرفعال"

        parts.append(
            f"🆔 `{row['id']}` | {status}\n"
            f"👤 {row['owner_name']}\n"
            f"🏦 `{row['account_number']}`\n"
            f"💳 سقف: `{fmt_amount(row['capacity'])}`\n"
            f"🟡 رزرو: `{fmt_amount(row['reserved_amount'])}`\n"
            f"🟢 پرداخت: `{fmt_amount(row['paid_amount'])}`\n"
            f"📊 باقی: `{fmt_amount(remaining)}`\n"
            f"⏰ ددلاین: `{deadline_display(row['deadline_at'])}`\n"
            "━━━━━━━━━━━━━━"
        )

        keyboard.append([
            InlineKeyboardButton(
                f"👥 رزروهای حساب #{row['id']}",
                callback_data=f"a_target_reservations_{row['id']}",
             style="primary")
        ])
        if row["active"]:
            keyboard.append([
                InlineKeyboardButton(
                    "✏️ تغییر شماره حساب",
                    callback_data=f"a_target_edit_{row['id']}",
                 style="primary"),
                InlineKeyboardButton(
                    "🗑 حذف حساب",
                    callback_data=f"a_target_delete_{row['id']}",
                 style="danger"),
            ])

    keyboard.append([
        InlineKeyboardButton("⬅️ بازگشت", callback_data="main", style="primary")
    ])

    await tracked_edit(query, context, 
        "\n".join(parts),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# ADMIN ACCOUNT MANAGEMENT
# =========================================================

async def admin_target_reservations(update, context, target_id):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()
    target = conn.execute(
        "SELECT * FROM payment_targets WHERE id = ?", (target_id,)
    ).fetchone()
    rows = conn.execute("""
        SELECT id, user_id, username, first_name, reservation_name,
               account_number_snapshot, amount, status, receipt_file_id, created_at
        FROM requests
        WHERE target_id = ?
          AND status IN ('reserved', 'paid', 'rejected')
        ORDER BY id DESC
        LIMIT 100
    """, (target_id,)).fetchall()
    conn.close()

    if not target:
        await tracked_edit(query, context, "❌ حساب پیدا نشد.", reply_markup=back_button())
        return

    if not rows:
        text = (
            "👥 *رزروهای این حساب*\n\n"
            f"🏦 `{target['account_number']}`\n"
            "هیچ رزروی برای این حساب ثبت نشده است."
        )
    else:
        parts = [
            "👥 *رزروهای این حساب*\n",
            f"🆔 حساب: `{target['id']}`",
            f"🏦 `{target['account_number']}`\n",
        ]
        status_names = {
            "reserved": "🟡 رزرو شده",
            "paid": "🟢 پرداخت شده",
            "rejected": "🔴 رد شده",
        }
        for row in rows:
            name = row["reservation_name"] or row["first_name"] or "-"
            username = f"@{row['username']}" if row["username"] else "-"
            receipt = "🧾 فیش ارسال شده" if row["receipt_file_id"] else "⚠️ بدون فیش"
            parts.append(
                f"🆔 درخواست: `{row['id']}`\n"
                f"👤 نام رزرو: {name}\n"
                f"🔹 یوزرنیم: {username}\n"
                f"🔢 User ID: `{row['user_id']}`\n"
                f"🏦 شماره هنگام رزرو: `{row['account_number_snapshot'] or target['account_number']}`\n"
                f"💰 مبلغ رزرو: `{fmt_amount(row['amount'])}` تومان\n"
                f"📌 وضعیت: {status_names.get(row['status'], row['status'])}\n"
                f"{receipt}\n"
                "━━━━━━━━━━━━━━"
            )
        text = "\n".join(parts)

    await tracked_edit(query, context, 
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ وضعیت حساب‌ها", callback_data="a_status", style="primary")],
            [InlineKeyboardButton("🏠 منوی اصلی", callback_data="main", style="primary")],
        ]),
    )


async def admin_edit_target_start(update, context, target_id):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()
    target = conn.execute(
        "SELECT id, account_number FROM payment_targets WHERE id = ? AND active = 1",
        (target_id,),
    ).fetchone()
    conn.close()

    if not target:
        await tracked_edit(query, context, 
            "❌ این حساب پیدا نشد یا قبلاً حذف شده است.",
            reply_markup=back_button(),
        )
        return

    clear_state(context)
    context.user_data["edit_target_id"] = target_id

    await tracked_edit(query, context, 
        "✏️ *تغییر شماره حساب*\n\n"
        f"🆔 حساب: `{target['id']}`\n"
        f"🏦 شماره فعلی: `{target['account_number']}`\n\n"
        "شماره حساب جدید را ارسال کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


async def process_edit_target(update, context):
    target_id = context.user_data.get("edit_target_id")
    if not target_id:
        return

    if not is_admin(update.effective_user.id):
        clear_state(context)
        return

    new_account = (update.message.text or "").strip()
    if not new_account or len(new_account) < 4 or len(new_account) > 100:
        await tracked_reply(update, context, "❌ شماره حساب نامعتبر است. دوباره وارد کنید.")
        return

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    target = conn.execute(
        "SELECT * FROM payment_targets WHERE id = ? AND active = 1", (target_id,)
    ).fetchone()
    if not target:
        conn.rollback()
        conn.close()
        clear_state(context)
        await tracked_reply(update, context, "❌ حساب پیدا نشد یا غیرفعال شده است.", reply_markup=admin_menu())
        return

    old_account = target["account_number"]
    if new_account == old_account:
        conn.rollback()
        conn.close()
        clear_state(context)
        await tracked_reply(update, context, "ℹ️ شماره حساب تغییری نکرد.", reply_markup=admin_menu())
        return

    conn.execute(
        "UPDATE payment_targets SET account_number = ? WHERE id = ?",
        (new_account, target_id),
    )

    affected = conn.execute("""
        SELECT id, user_id, amount, reservation_name, receipt_file_id
        FROM requests
        WHERE target_id = ?
          AND status = 'reserved'
    """, (target_id,)).fetchall()

    conn.commit()
    conn.close()
    clear_state(context)

    await tracked_reply(update, context, 
        "✅ *شماره حساب تغییر کرد*\n\n"
        f"🆔 حساب: `{target_id}`\n"
        f"🏦 قبلی: `{old_account}`\n"
        f"🏦 جدید: `{new_account}`\n\n"
        f"📢 تعداد رزروهای فعال که اطلاع‌رسانی می‌شوند: `{len(affected)}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )

    for row in affected:
        try:
            await tracked_send_message(context.bot, 
                row["user_id"],
                "⚠️ *اطلاعیه تغییر شماره حساب*\n\n"
                f"🆔 درخواست: `{row['id']}`\n"
                f"💰 مبلغ رزرو: `{fmt_amount(row['amount'])}` تومان\n"
                f"🏦 شماره حساب قبلی: `{old_account}`\n"
                f"🏦 شماره حساب جدید: `{new_account}`\n\n"
                "لطفاً برای این درخواست، شماره حساب جدید را ملاک قرار دهید.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


async def admin_delete_target(update, context, target_id):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()
    target = conn.execute(
        "SELECT * FROM payment_targets WHERE id = ? AND active = 1", (target_id,)
    ).fetchone()
    if not target:
        conn.close()
        await tracked_edit(query, context, "❌ حساب پیدا نشد یا قبلاً حذف شده است.", reply_markup=back_button())
        return

    pending_receipt = conn.execute("""
        SELECT COUNT(*) AS c
        FROM requests
        WHERE target_id = ?
          AND status = 'reserved'
          AND receipt_file_id IS NOT NULL
    """, (target_id,)).fetchone()["c"]
    conn.close()

    if pending_receipt:
        await tracked_edit(query, context, 
            "⚠️ *حذف حساب انجام نشد*\n\n"
            f"برای این حساب `{pending_receipt}` درخواست وجود دارد که فیش آن ارسال شده ولی هنوز بررسی نشده است.\n\n"
            "ابتدا فیش‌های آن حساب را بررسی کنید، سپس حساب را حذف کنید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧾 فیش‌های در انتظار تایید", callback_data="a_pending", style="success")],
                [InlineKeyboardButton("⬅️ وضعیت حساب‌ها", callback_data="a_status", style="primary")],
            ]),
        )
        return

    await tracked_edit(query, context, 
        "⚠️ *تأیید حذف حساب*\n\n"
        f"🆔 `{target['id']}`\n"
        f"👤 {target['owner_name']}\n"
        f"🏦 `{target['account_number']}`\n\n"
        "با حذف حساب، رزروهای بدون فیش آزاد و دوباره در صف انتظار قرار می‌گیرند و به کاربران اطلاع داده می‌شود.\n\n"
        "تاریخچه درخواست‌ها حذف نمی‌شود.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 بله، حذف شود", callback_data=f"a_target_delete_confirm_{target_id}", style="danger")],
            [InlineKeyboardButton("❌ انصراف", callback_data="a_status", style="danger")],
        ]),
    )


async def admin_delete_target_confirm(update, context, target_id):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    target = conn.execute(
        "SELECT * FROM payment_targets WHERE id = ? AND active = 1", (target_id,)
    ).fetchone()
    if not target:
        conn.rollback()
        conn.close()
        await tracked_edit(query, context, "❌ حساب پیدا نشد یا قبلاً حذف شده است.", reply_markup=back_button())
        return

    pending_receipt = conn.execute("""
        SELECT COUNT(*) AS c FROM requests
        WHERE target_id = ? AND status = 'reserved' AND receipt_file_id IS NOT NULL
    """, (target_id,)).fetchone()["c"]
    if pending_receipt:
        conn.rollback()
        conn.close()
        await tracked_edit(query, context, 
            "❌ حذف متوقف شد؛ هنوز فیش بررسی‌نشده برای این حساب وجود دارد.",
            reply_markup=back_button(),
        )
        return

    affected = conn.execute("""
        SELECT id, user_id, amount, reservation_name
        FROM requests
        WHERE target_id = ? AND status = 'reserved' AND receipt_file_id IS NULL
    """, (target_id,)).fetchall()

    now = now_iso()
    for row in affected:
        conn.execute("""
            UPDATE requests
            SET target_id = NULL,
                status = 'waiting',
                updated_at = ?,
                next_reminder_at = NULL
            WHERE id = ? AND status = 'reserved' AND receipt_file_id IS NULL
        """, (now, row["id"]))

    conn.execute(
        "UPDATE payment_targets SET active = 0, reserved_amount = 0 WHERE id = ?",
        (target_id,),
    )
    conn.commit()
    conn.close()

    await tracked_edit(query, context, 
        "✅ *حساب حذف شد*\n\n"
        f"🆔 `{target_id}`\n"
        f"🏦 `{target['account_number']}`\n"
        f"📢 رزروهای آزادشده: `{len(affected)}`\n\n"
        "حساب از تخصیص‌های جدید خارج شد و تاریخچه آن حفظ شده است.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )

    for row in affected:
        try:
            await tracked_send_message(context.bot, 
                row["user_id"],
                "⚠️ *اطلاعیه حذف شماره حساب*\n\n"
                f"🆔 درخواست: `{row['id']}`\n"
                f"💰 مبلغ رزرو: `{fmt_amount(row['amount'])}` تومان\n\n"
                "شماره حسابی که برای شما رزرو شده بود از دسترس خارج شده است.\n"
                "درخواست شما لغو نشده و دوباره در صف انتظار قرار گرفت؛ به محض پیدا شدن حساب مناسب، شماره حساب جدید برای شما ارسال می‌شود.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=user_menu(),
            )
        except Exception:
            pass


# =========================================================
# ADMIN PAYMENT PENDING (NO RECEIPT)
# =========================================================

async def admin_payment_pending(update, context):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()
    rows = conn.execute("""
        SELECT id, user_id, username, first_name, reservation_name, amount, created_at
        FROM requests
        WHERE status = 'reserved'
          AND receipt_file_id IS NULL
        ORDER BY id ASC
        LIMIT 50
    """).fetchall()
    conn.close()

    if not rows:
        await tracked_edit(query, context, 
            "💳 *درخواست‌های در انتظار واریز*\n\n"
            "هیچ درخواستی در انتظار واریز نیست.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_button(),
        )
        return

    keyboard = []
    for row in rows:
        keyboard.append([
            InlineKeyboardButton(
                f"💳 #{row['id']} | {row['reservation_name'] or row['first_name'] or '-'} | {fmt_amount(row['amount'])}",
                callback_data=f"pp_{row['id']}",
             style="primary")
        ])

    keyboard.append([
        InlineKeyboardButton("⬅️ بازگشت", callback_data="main", style="primary")
    ])

    await tracked_edit(query, context, 
        "💳 *درخواست‌های در انتظار واریز*\n\n"
        "این افراد حساب گرفته‌اند ولی هنوز فیش ارسال نکرده‌اند:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_payment_pending_item(update, context, request_id):
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()
    row = conn.execute("""
        SELECT * FROM requests
        WHERE id = ?
          AND status = 'reserved'
          AND receipt_file_id IS NULL
    """, (request_id,)).fetchone()
    conn.close()

    if not row:
        await tracked_edit(query, context, 
            "❌ درخواست پیدا نشد یا فیش آن ارسال شده است.",
            reply_markup=back_button(),
        )
        return

    text = (
        "💳 *درخواست در انتظار واریز*\n\n"
        f"🆔 `{row['id']}`\n"
        f"👤 نام رزرو: {row['reservation_name'] or row['first_name'] or '-'}\n"
        f"🔹 @{row['username'] or '-'}\n"
        f"🔢 `{row['user_id']}`\n"
        f"🏦 شماره حساب: `{row['account_number_snapshot'] or '-'}`\n"
        f"💰 `{fmt_amount(row['amount'])}` تومان\n\n"
        "⚠️ هنوز فیش ارسال نشده است."
    )

    await tracked_edit(query, context, 
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ برگشت", callback_data="a_payment_pending", style="primary")],
            [InlineKeyboardButton("🏠 منوی اصلی", callback_data="main", style="primary")],
        ]),
    )


# =========================================================
# ADMIN PENDING
# =========================================================

async def admin_pending(update, context):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()

    rows = conn.execute("""
        SELECT
            id,
            amount,
            receipt_file_id
        FROM requests
        WHERE status = 'reserved'
          AND receipt_file_id IS NOT NULL
        ORDER BY id ASC
    """).fetchall()

    conn.close()

    if not rows:
        await tracked_edit(query, context, 
            "⏳ *پرداخت‌های در انتظار*\n\n"
            "هیچ پرداختی وجود ندارد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_button(),
        )
        return

    keyboard = []

    for row in rows:
        icon = (
            "🧾"
            if row["receipt_file_id"]
            else "⚠️"
        )

        keyboard.append([
            InlineKeyboardButton(
                f"{icon} #{row['id']} | "
                f"{fmt_amount(row['amount'])}",
                callback_data=f"p_{row['id']}",
             style="primary"),
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ بازگشت",
            callback_data="main",
         style="primary"),
    ])

    await tracked_edit(query, context, 
        "⏳ *پرداخت‌های در انتظار*\n\n"
        "یک مورد را انتخاب کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def pending_item(
    update,
    context,
    request_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM requests
        WHERE id = ?
    """, (
        request_id,
    )).fetchone()

    conn.close()

    if not row:
        await tracked_edit(query, context, 
            "❌ درخواست پیدا نشد.",
            reply_markup=back_button(),
        )
        return

    receipt = (
        "🧾 فیش دریافت شده"
        if row["receipt_file_id"]
        else "⚠️ فیش ارسال نشده"
    )

    text = (
        "⏳ *جزئیات پرداخت*\n\n"
        f"🆔 `{row['id']}`\n"
        f"👤 نام رزرو: {row['reservation_name'] or row['first_name'] or '-'}\n"
        f"🔹 @{row['username'] or '-'}\n"
        f"🔢 `{row['user_id']}`\n"
        f"🏦 شماره هنگام رزرو: `{row['account_number_snapshot'] or '-'}`\n"
        f"💰 `{fmt_amount(row['amount'])}` تومان\n"
        f"{receipt}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تأیید",
                callback_data=f"ok_{request_id}",
             style="success"),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"no_{request_id}",
             style="danger"),
        ],
        [
            InlineKeyboardButton(
                "⬅️ برگشت",
                callback_data="a_pending",
             style="primary"),
        ],
    ])

    await tracked_edit(query, context, 
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def assign_waiting_requests(context, conn):
    assigned = []
    while True:
        request = conn.execute("SELECT * FROM requests WHERE status = 'waiting' ORDER BY id ASC LIMIT 1").fetchone()
        if not request:
            break
        target = conn.execute("""
            SELECT * FROM payment_targets
            WHERE active = 1 AND (deadline_at IS NULL OR deadline_at > ?)
              AND (capacity - reserved_amount - paid_amount) >= ?
            ORDER BY CASE WHEN deadline_at IS NULL THEN 1 ELSE 0 END ASC, deadline_at ASC, id ASC
            LIMIT 1
        """, (now_iso(), request['amount'])).fetchone()
        if not target:
            break
        now = now_iso()
        conn.execute("""UPDATE requests SET target_id=?, account_number_snapshot=?, status='reserved', updated_at=?, next_reminder_at=? WHERE id=? AND status='waiting'""", (target['id'], target['account_number'], now, reminder_due(now), request['id']))
        conn.execute("UPDATE payment_targets SET reserved_amount=reserved_amount+? WHERE id=?", (request['amount'], target['id']))
        assigned.append((dict(request), dict(target)))
    return assigned


# =========================================================
# ADD ACCOUNT
# =========================================================

async def add_account_menu(
    update,
    context,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    clear_state(context)

    context.user_data[
        "new_account"
    ] = True

    await tracked_edit(query, context, 
        "➕ *افزودن حساب*\n\n"
        "فرمت:\n"
        "`نام|شماره‌حساب|سقف|ددلاین`\n\n"
        "ددلاین به وقت ایران: `YYYY-MM-DD HH:MM`\n\n"
        "مثال:\n"
        "`علی امینی|6037991234567890|11.5|2026-09-20 18:00`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


async def process_new_account(
    update,
    context,
):
    if not context.user_data.get(
        "new_account"
    ):
        return

    if not is_admin(update.effective_user.id):
        return

    parts = update.message.text.split("|")

    if len(parts) != 4:
        await tracked_reply(update, context, 
            "❌ فرمت اشتباه است.\n\n"
            "`نام|شماره‌حساب|سقف|ددلاین`\n\n"
            "ددلاین: `YYYY-MM-DD HH:MM` به وقت ایران",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    owner = parts[0].strip()
    account = parts[1].strip()
    capacity = parse_amount(parts[2])
    deadline_at = parse_deadline(parts[3])

    if not owner:
        await tracked_reply(update, context, 
            "❌ نام خالی است."
        )
        return

    if not account:
        await tracked_reply(update, context, 
            "❌ شماره حساب خالی است."
        )
        return

    if capacity is None:
        await tracked_reply(update, context, 
            "❌ سقف نامعتبر است."
        )
        return

    if deadline_at is None:
        await tracked_reply(update, context, 
            "❌ ددلاین نامعتبر است.\n\nفرمت صحیح: `YYYY-MM-DD HH:MM` به وقت ایران",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if parse_iso(deadline_at) <= datetime.now(timezone.utc):
        await tracked_reply(update, context, "❌ ددلاین باید در آینده باشد.")
        return

    conn = db()

    cursor = conn.execute("""
        INSERT INTO payment_targets (
            owner_name, account_number, capacity, reserved_amount,
            paid_amount, active, deadline_at, created_at
        )
        VALUES (?, ?, ?, 0, 0, 1, ?, ?)
    """, (owner, account, capacity, deadline_at, now_iso()))

    target_id = cursor.lastrowid

    assigned = await assign_waiting_requests(context, conn)

    conn.commit()
    conn.close()

    clear_state(context)

    await tracked_reply(update, context, 
        "✅ *حساب اضافه شد*\n\n"
        f"🆔 `{target_id}`\n"
        f"👤 `{owner}`\n"
        f"🏦 `{account}`\n"
        f"💳 `{fmt_amount(capacity)}` تومان\n"
        f"⏰ ددلاین: `{deadline_display(deadline_at)}`\n\n"
        f"🔄 درخواست اختصاص‌یافته: `{len(assigned)}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )

    # اطلاع کاربران
    for request, target in assigned:
        try:
            await tracked_send_message(context.bot, 
                request["user_id"],
                "🎉 *حساب برای شما آماده شد!*\n\n"
                f"🆔 درخواست: `{request['id']}`\n"
                f"👤 صاحب حساب: `{target['owner_name']}`\n"
                f"🏦 شماره حساب:\n"
                f"`{target['account_number']}`\n\n"
                f"💰 مبلغ:\n"
                f"`{fmt_amount(request['amount'])}` تومان\n\n"
                "پس از پرداخت، فیش را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🧾 ارسال فیش",
                            callback_data="u_receipt",
                         style="primary"),
                    ],
                ]),
            )
        except Exception:
            pass


# =========================================================
# HISTORY
# =========================================================

async def history_menu(update, context):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()

    rows = conn.execute("""
        SELECT id, owner_name
        FROM payment_targets
        ORDER BY id DESC
    """).fetchall()

    conn.close()

    if not rows:
        await tracked_edit(query, context, 
            "📜 هیچ حسابی وجود ندارد.",
            reply_markup=back_button(),
        )
        return

    keyboard = []

    for row in rows:
        keyboard.append([
            InlineKeyboardButton(
                f"🏦 {row['owner_name']} #{row['id']}",
                callback_data=f"h_{row['id']}",
             style="primary"),
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ بازگشت",
            callback_data="main",
         style="primary"),
    ])

    await tracked_edit(query, context, 
        "📜 *انتخاب حساب:*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def history_item(
    update,
    context,
    target_id,
):
    query = update.callback_query

    await query.answer()

    if not is_admin(query.from_user.id):
        return

    conn = db()

    target = conn.execute("""
        SELECT *
        FROM payment_targets
        WHERE id = ?
    """, (
        target_id,
    )).fetchone()

    rows = conn.execute("""
        SELECT id, amount, status, reservation_name, account_number_snapshot, user_id, username, receipt_file_id
        FROM requests
        WHERE target_id = ?
        ORDER BY id DESC
        LIMIT 30
    """, (
        target_id,
    )).fetchall()

    conn.close()

    if not target:
        await tracked_edit(query, context, 
            "❌ حساب پیدا نشد.",
            reply_markup=back_button(),
        )
        return

    status_names = {
        "reserved": "🟡 رزرو",
        "paid": "🟢 پرداخت",
        "rejected": "🔴 رد",
        "waiting": "⏳ انتظار",
        "cancelled": "⚫ لغو شده",
    }

    parts = [
        "📜 *تاریخچه حساب*\n",
        f"🆔 `{target['id']}`",
        f"👤 `{target['owner_name']}`",
        f"🏦 `{target['account_number']}`",
        "",
    ]

    if not rows:
        parts.append(
            "تراکنشی ثبت نشده است."
        )

    else:
        for row in rows:
            parts.append(
                f"🆔 `{row['id']}` | "
                f"👤 {row['reservation_name'] or "-"} | "
                f"🏦 `{row['account_number_snapshot'] or target['account_number']}` | "
                f"💰 `{fmt_amount(row['amount'])}` | "
                f"{status_names.get(row['status'], row['status'])}"
            )

    await tracked_edit(query, context, 
        "\n".join(parts),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback_router(
    update,
    context,
):
    query = update.callback_query
    data = query.data

    # فقط دکمه‌های آخرین پیام قابل استفاده‌اند.
    # با این کار کلیک روی دکمه‌های پیام‌های قدیمی یک خطای واضح نشان می‌دهد.
    if query.message is not None:
        user_id = query.from_user.id
        latest_message_id = LATEST_MESSAGE_IDS.get(user_id)
        if latest_message_id is None:
            LATEST_MESSAGE_IDS[user_id] = query.message.message_id
        elif query.message.message_id != latest_message_id:
            try:
                await query.answer(
                    "لطفاً از آخرین پیام استفاده کنید",
                    show_alert=True,
                )
            except Exception:
                pass
            return

    # -----------------------------------------------------
    # جواب فوری به Telegram
    # -----------------------------------------------------

    try:
        await query.answer()
    except Exception:
        pass

    # -----------------------------------------------------
    # MAIN
    # -----------------------------------------------------

    if data == "main":
        await show_main_menu(
            update,
            context,
            edit=True,
        )
        return

    # -----------------------------------------------------
    # USER PANEL
    # -----------------------------------------------------

    if data == "u_panel":
        clear_state(context)

        await tracked_edit(query, context, 
            "👤 *پنل کاربر*\n\n"
            "گزینه موردنظر را انتخاب کنید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=user_menu(),
        )
        return

    # -----------------------------------------------------
    # GET ACCOUNT
    # -----------------------------------------------------

    if data == "u_get":
        await get_account_menu(
            update,
            context,
        )
        return

    # -----------------------------------------------------
    # REQUESTS MENU
    # -----------------------------------------------------

    if data == "u_requests":
        await my_requests_menu(
            update,
            context,
        )
        return

    # -----------------------------------------------------
    # REQUESTS BY STATUS
    # -----------------------------------------------------

    if data == "ur_rejected":
        await my_requests_by_status(
            update,
            context,
            "rejected",
        )
        return

    if data == "ur_cancelled":
        await my_requests_by_status(
            update,
            context,
            "cancelled",
        )
        return

    if data == "ur_paid":
        await my_requests_by_status(
            update,
            context,
            "paid",
        )
        return

    if data == "ur_waiting":
        await my_requests_by_status(
            update,
            context,
            "waiting",
        )
        return

    if data == "ur_payment_pending":
        await my_requests_by_status(
            update,
            context,
            "payment_pending",
        )
        return

    if data == "ur_reserved":
        await my_requests_by_status(
            update,
            context,
            "reserved",
        )
        return

    # -----------------------------------------------------
    # A CONTENT RECEIPTS
    # -----------------------------------------------------

    if data == "c_receipts":
        await a_content_receipts(update, context)
        return

    if data.startswith("c_receipt_"):
        try:
            request_id = int(data[len("c_receipt_"):])
        except ValueError:
            return
        await a_content_receipt_item(update, context, request_id)
        return

    # -----------------------------------------------------
    # RECEIPT
    # -----------------------------------------------------

    if data == "u_receipt":
        await receipt_menu(
            update,
            context,
        )
        return

    if data.startswith("receipt_select_"):
        try:
            request_id = int(data[len("receipt_select_"):])
        except ValueError:
            return
        await receipt_select(update, context, request_id)
        return

    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    if data == "u_help":

        await tracked_edit(query, context, 
            "ℹ️ *راهنما*\n\n"
            "💰 مبلغ را وارد کنید تا حساب مناسب اختصاص داده شود.\n\n"
            "🧾 پس از پرداخت، فیش را ارسال کنید.\n\n"
            "📋 از بخش درخواست‌های من می‌توانید وضعیت را ببینید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_button(),
        )
        return

    # -----------------------------------------------------
    # ADMIN ADD
    # -----------------------------------------------------

    if data == "a_add":
        await add_account_menu(
            update,
            context,
        )
        return

    # -----------------------------------------------------
    # ADMIN STATUS
    # -----------------------------------------------------

    if data == "a_status":
        await admin_status(
            update,
            context,
        )
        return

    # -----------------------------------------------------
    # ADMIN ACCOUNT MANAGEMENT
    # -----------------------------------------------------

    if data.startswith("a_target_delete_confirm_"):
        try:
            target_id = int(data[len("a_target_delete_confirm_"):])
        except ValueError:
            return
        await admin_delete_target_confirm(update, context, target_id)
        return

    if data.startswith("a_target_reservations_"):
        try:
            target_id = int(data[len("a_target_reservations_"):])
        except ValueError:
            return
        await admin_target_reservations(update, context, target_id)
        return

    if data.startswith("a_target_edit_"):
        try:
            target_id = int(data[len("a_target_edit_"):])
        except ValueError:
            return
        await admin_edit_target_start(update, context, target_id)
        return

    if data.startswith("a_target_delete_"):
        try:
            target_id = int(data[len("a_target_delete_"):])
        except ValueError:
            return
        await admin_delete_target(update, context, target_id)
        return

    # -----------------------------------------------------
    # ADMIN PENDING
    # -----------------------------------------------------

    if data == "a_payment_pending":
        await admin_payment_pending(update, context)
        return

    if data.startswith("pp_"):
        try:
            request_id = int(data[3:])
        except ValueError:
            return
        await admin_payment_pending_item(update, context, request_id)
        return

    if data == "a_pending":
        await admin_pending(
            update,
            context,
        )
        return

    # -----------------------------------------------------
    # ADMIN HISTORY
    # -----------------------------------------------------

    if data == "a_history":
        await history_menu(
            update,
            context,
        )
        return


    # -----------------------------------------------------
    # PAYMENT REMINDER - CONTINUE / CANCEL
    # -----------------------------------------------------

    if data.startswith("continue_payment_"):
        try:
            request_id = int(data[len("continue_payment_"):])
        except ValueError:
            return

        conn = db()
        row = conn.execute("""
            SELECT * FROM requests
            WHERE id = ? AND user_id = ?
        """, (request_id, query.from_user.id)).fetchone()

        if row and row['status'] == 'reserved' and not row['receipt_file_id']:
            conn.execute("""
                UPDATE requests
                SET next_reminder_at = ?, updated_at = ?
                WHERE id = ? AND status = 'reserved' AND receipt_file_id IS NULL
            """, (next_reminder_after_now(), now_iso(), request_id))
            conn.commit()
        conn.close()

        if not row or row['status'] != 'reserved' or row['receipt_file_id']:
            await tracked_edit(query, context, "❌ این درخواست دیگر در انتظار واریز نیست.")
            return

        await tracked_edit(query, context, 
            "▶️ *ادامه پرداخت*\n\n"
            f"🆔 درخواست: `{request_id}`\n"
            "درخواست شما فعال ماند. پس از پرداخت، فیش را ارسال کنید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🧾 ارسال فیش", callback_data=f"r_{request_id}", style="primary")],
                [InlineKeyboardButton("📋 درخواست‌های من", callback_data="u_requests", style="primary")],
            ]),
        )
        return

    if data.startswith("cancel_payment_"):
        try:
            request_id = int(data[len("cancel_payment_"):])
        except ValueError:
            return

        conn = db()
        row = conn.execute("""
            SELECT * FROM requests
            WHERE id = ? AND user_id = ?
        """, (request_id, query.from_user.id)).fetchone()

        if not row or row['status'] != 'reserved' or row['receipt_file_id']:
            conn.close()
            await tracked_edit(query, context, "❌ این درخواست دیگر قابل لغو نیست.")
            return

        conn.execute("""
            UPDATE requests
            SET status = 'cancelled',
                updated_at = ?,
                next_reminder_at = NULL
            WHERE id = ? AND user_id = ? AND status = 'reserved' AND receipt_file_id IS NULL
        """, (now_iso(), request_id, query.from_user.id))

        if row['target_id']:
            conn.execute("""
                UPDATE payment_targets
                SET reserved_amount = MAX(0, reserved_amount - ?)
                WHERE id = ?
            """, (row['amount'], row['target_id']))

        conn.commit()
        conn.close()

        await tracked_edit(query, context, 
            "❌ *درخواست لغو شد*\n\n"
            f"🆔 درخواست: `{request_id}`\n"
            "رزرو این درخواست آزاد شد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=user_menu(),
        )

        for admin_id in ADMIN_IDS:
            try:
                await tracked_send_message(context.bot, 
                    admin_id,
                    f"❌ درخواست `{request_id}` توسط کاربر لغو شد.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
        return

    # -----------------------------------------------------
    # APPROVE
    # -----------------------------------------------------

    if data.startswith("ok_"):

        try:
            request_id = int(
                data[3:]
            )
        except ValueError:
            return

        await approve(
            update,
            context,
            request_id,
        )
        return

    # -----------------------------------------------------
    # REJECT
    # -----------------------------------------------------

    if data.startswith("no_"):

        try:
            request_id = int(
                data[3:]
            )
        except ValueError:
            return

        await reject(
            update,
            context,
            request_id,
        )
        return

    # -----------------------------------------------------
    # PENDING ITEM
    # -----------------------------------------------------

    if data.startswith("p_"):

        try:
            request_id = int(
                data[2:]
            )
        except ValueError:
            return

        await pending_item(
            update,
            context,
            request_id,
        )
        return

    # -----------------------------------------------------
    # HISTORY ITEM
    # -----------------------------------------------------

    if data.startswith("h_"):

        try:
            target_id = int(
                data[2:]
            )
        except ValueError:
            return

        await history_item(
            update,
            context,
            target_id,
        )
        return


# =========================================================
# OLD COMMAND: GETACCOUNT
# =========================================================

async def command_getaccount(
    update,
    context,
):
    if not context.args:
        await tracked_reply(update, context, 
            "مثال:\n"
            "`/getaccount 11.5`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    amount = parse_amount(
        context.args[0]
    )

    if amount is None:
        await tracked_reply(update, context, 
            "❌ مبلغ نامعتبر است."
        )
        return

    context.user_data["pending_amount"] = amount
    context.user_data["reservation_name"] = True

    await tracked_reply(update, context, 
        "👤 لطفاً *نام و نام خانوادگی* خود را برای ثبت رزرو وارد کنید:\n\n"
        "مثال: `علی رضایی`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


# =========================================================
# OLD COMMAND: RECEIPT
# =========================================================

async def command_receipt(update, context):
    user_id = update.effective_user.id
    conn = db()
    rows = conn.execute("""
        SELECT id, reservation_name, first_name, amount
        FROM requests
        WHERE user_id = ?
          AND status = 'reserved'
          AND receipt_file_id IS NULL
        ORDER BY id DESC
        LIMIT 50
    """, (user_id,)).fetchall()
    conn.close()

    if not rows:
        await tracked_reply(update, context, 
            "🧾 *ارسال فیش*\\n\\n"
            "❌ هیچ رزروی که هنوز فیش آن ارسال نشده باشد پیدا نشد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=user_menu(),
        )
        return

    keyboard = []
    for row in rows:
        name = row["reservation_name"] or row["first_name"] or "بدون نام"
        keyboard.append([
            InlineKeyboardButton(
                f"🧾 #{row['id']} | {name} | {fmt_amount(row['amount'])} تومان",
                callback_data=f"receipt_select_{row['id']}",
             style="primary")
        ])

    keyboard.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="main", style="primary")])

    await tracked_reply(update, context, 
        "🧾 *ارسال فیش*\\n\\n"
        "یکی از رزروهای بدون فیش را انتخاب کنید:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(
    update,
    context,
):
    if await receive_adpass_password(update, context):
        return

    if context.user_data.get("edit_target_id"):
        await process_edit_target(update, context)
        return

    if context.user_data.get("reservation_name"):
        await receive_reservation_name(
            update,
            context,
        )
        return

    if context.user_data.get("amount"):
        await receive_amount(
            update,
            context,
        )
        return

    if context.user_data.get("new_account"):
        await process_new_account(
            update,
            context,
        )
        return


# =========================================================
# PAYMENT REMINDERS
# =========================================================

async def send_payment_reminder(bot, row):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ لغو درخواست", callback_data=f"cancel_payment_{row['id']}", style="danger"),
            InlineKeyboardButton("▶️ ادامه", callback_data=f"continue_payment_{row['id']}", style="primary"),
        ],
    ])

    text = (
        "⏰ *یادآوری پرداخت*\n\n"
        f"🆔 درخواست: `{row['id']}`\n"
        f"💰 مبلغ: `{fmt_amount(row['amount'])}` تومان\n\n"
        "از زمان درخواست شما ۲۴ ساعت گذشته و هنوز فیش پرداخت ارسال نشده است.\n\n"
        "اگر می‌خواهید این درخواست لغو شود، «لغو درخواست» را بزنید.\n"
        "اگر هنوز قصد پرداخت دارید، «ادامه» را بزنید."
    )

    await tracked_send_message(bot, 
        row['user_id'],
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def payment_reminder_loop(bot):
    while True:
        try:
            conn = db()
            now = datetime.now(timezone.utc).isoformat()
            rows = conn.execute("""
                SELECT id, user_id, amount, next_reminder_at
                FROM requests
                WHERE status = 'reserved'
                  AND receipt_file_id IS NULL
                  AND next_reminder_at IS NOT NULL
                  AND next_reminder_at <= ?
                ORDER BY id ASC
                LIMIT 100
            """, (now,)).fetchall()

            for row in rows:
                # ابتدا زمان یادآوری بعدی را جلو می‌بریم تا در اجرای همزمان دوباره ارسال نشود.
                conn.execute("""
                    UPDATE requests
                    SET next_reminder_at = ?
                    WHERE id = ?
                      AND status = 'reserved'
                      AND receipt_file_id IS NULL
                """, (next_reminder_after_now(), row['id']))

            conn.commit()
            conn.close()

            for row in rows:
                try:
                    await send_payment_reminder(bot, row)
                except Exception:
                    logger.exception("Failed to send payment reminder for request %s", row['id'])

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payment reminder loop failed")

        await asyncio.sleep(REMINDER_INTERVAL_SECONDS)


async def post_init(application):
    application.bot_data["payment_reminder_task"] = asyncio.create_task(
        payment_reminder_loop(application.bot)
    )


async def post_shutdown(application):
    task = application.bot_data.get("payment_reminder_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update,
    context,
):
    logger.error(
        "Bot error",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN در ابتدای فایل تنظیم نشده است.")

    if not ADMIN_IDS:
        raise RuntimeError("حداقل یک ADMIN_ID باید در ابتدای فایل تنظیم شود.")

    init_db()

    bot_request = HTTPXRequest(
        connection_pool_size=100,
        pool_timeout=5.0,
        connect_timeout=5.0,
        read_timeout=30.0,
        write_timeout=30.0,
    )

    get_updates_request = HTTPXRequest(
        connection_pool_size=100,
        pool_timeout=5.0,
        connect_timeout=5.0,
        read_timeout=35.0,
        write_timeout=30.0,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(bot_request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "adpass",
            command_adpass,
        )
    )

    application.add_handler(
        CommandHandler(
            "getaccount",
            command_getaccount,
        )
    )

    application.add_handler(
        CommandHandler(
            "receipt",
            command_receipt,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.ALL,
            receive_receipt,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("ربات در حال اجراست...")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
