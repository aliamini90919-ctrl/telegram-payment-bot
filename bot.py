import logging
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
}

DB_PATH = os.getenv(
    "DB_PATH",
    "payments.db",
)


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

    # سرعت و concurrency بهتر SQLite
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
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        amount INTEGER NOT NULL,
        target_id INTEGER,
        status TEXT NOT NULL DEFAULT 'waiting',
        receipt_file_id TEXT,
        receipt_type TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_requests_user
        ON requests(user_id);

    CREATE INDEX IF NOT EXISTS idx_requests_status
        ON requests(status);

    CREATE INDEX IF NOT EXISTS idx_requests_target
        ON requests(target_id);

    CREATE INDEX IF NOT EXISTS idx_targets_active
        ON payment_targets(active);
    """)

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
    }

    for column, sql in target_migrations.items():
        if column not in target_columns:
            try:
                conn.execute(sql)
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
    value = str(value).strip()

    value = value.replace(",", "")
    value = value.replace("٬", "")
    value = value.replace(" ", "")

    try:
        amount = Decimal(value)

        if amount <= 0:
            return None

        if amount != amount.to_integral_value():
            return None

        return int(amount)

    except (InvalidOperation, ValueError):
        return None


def fmt_amount(amount):
    return f"{int(amount):,}"


def is_admin(user_id):
    return user_id in ADMIN_IDS


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
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 درخواست‌های من",
                callback_data="u_requests",
            ),
            InlineKeyboardButton(
                "🧾 ارسال فیش",
                callback_data="u_receipt",
            ),
        ],
        [
            InlineKeyboardButton(
                "ℹ️ راهنما",
                callback_data="u_help",
            ),
        ],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ افزودن حساب",
                callback_data="a_add",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 وضعیت حساب‌ها",
                callback_data="a_status",
            ),
            InlineKeyboardButton(
                "⏳ در انتظار",
                callback_data="a_pending",
            ),
        ],
        [
            InlineKeyboardButton(
                "📜 تاریخچه",
                callback_data="a_history",
            ),
        ],
        [
            InlineKeyboardButton(
                "👤 پنل کاربر",
                callback_data="u_panel",
            ),
        ],
    ])


def back_button():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ بازگشت",
                callback_data="main",
            ),
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

    keyboard = (
        admin_menu()
        if is_admin(user.id)
        else user_menu()
    )

    text = menu_text(user)

    if edit:
        await update.callback_query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )


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

    # مهم‌ترین قسمت:
    # بلافاصله به Telegram اعلام می‌کنیم کلیک دریافت شد.
    await query.answer()

    clear_state(context)

    context.user_data["amount"] = True

    await query.edit_message_text(
        "💰 *دریافت حساب*\n\n"
        "مبلغ موردنظر را به تومان وارد کنید.\n\n"
        "مثال:\n"
        "`50000000`",
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
        await update.message.reply_text(
            "❌ مبلغ نامعتبر است.\n\n"
            "مثال:\n"
            "`50000000`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    context.user_data.pop(
        "amount",
        None,
    )

    await create_request(
        update,
        context,
        amount,
    )


async def create_request(
    update,
    context,
    amount,
):
    user = update.effective_user

    conn = db()

    # یک Query برای پیدا کردن حساب مناسب
    targets = conn.execute("""
        SELECT *
        FROM payment_targets
        WHERE active = 1
        AND (
            capacity
            - reserved_amount
            - paid_amount
        ) >= ?
        ORDER BY id ASC
        LIMIT 1
    """, (
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
                amount,
                target_id,
                status,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, 'reserved', ?, ?
            )
        """, (
            user.id,
            user.username,
            user.first_name,
            amount,
            targets["id"],
            created,
            created,
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

        await update.message.reply_text(
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
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📋 درخواست‌های من",
                        callback_data="u_requests",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ منوی اصلی",
                        callback_data="main",
                    ),
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
            amount,
            status,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, 'waiting', ?, ?
        )
    """, (
        user.id,
        user.username,
        user.first_name,
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

    await update.message.reply_text(
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
        f"👤 {user.first_name or '-'}\n"
        f"🔹 @{user.username or '-'}\n"
        f"🔢 `{user.id}`\n"
        f"💰 `{fmt_amount(amount)}` تومان\n\n"
        "⚠️ حساب مناسب موجود نیست."
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                admin_text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass


# =========================================================
# MY REQUESTS
# =========================================================

async def my_requests(update, context):
    query = update.callback_query

    await query.answer()

    conn = db()

    rows = conn.execute("""
        SELECT
            id,
            amount,
            status,
            receipt_file_id
        FROM requests
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
    """, (
        update.effective_user.id,
    )).fetchall()

    conn.close()

    status_names = {
        "waiting": "⏳ انتظار حساب",
        "reserved": "🟡 در انتظار تأیید",
        "paid": "🟢 تأیید شده",
        "rejected": "🔴 رد شده",
    }

    if not rows:
        text = (
            "📋 *درخواست‌های من*\n\n"
            "درخواستی ثبت نشده است."
        )

    else:
        parts = [
            "📋 *درخواست‌های من*\n"
        ]

        for row in rows:
            receipt = (
                "🧾 فیش دریافت شده"
                if row["receipt_file_id"]
                else "📎 بدون فیش"
            )

            parts.append(
                f"🆔 `{row['id']}`\n"
                f"💰 `{fmt_amount(row['amount'])}` تومان\n"
                f"📌 {status_names.get(row['status'], row['status'])}\n"
                f"{receipt}\n"
                "━━━━━━━━━━━━━━"
            )

        text = "\n".join(parts)

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


# =========================================================
# RECEIPT
# =========================================================

async def receipt_menu(update, context):
    query = update.callback_query

    await query.answer()

    clear_state(context)

    context.user_data[
        "receipt_id"
    ] = True

    await query.edit_message_text(
        "🧾 *ارسال فیش*\n\n"
        "شناسه درخواست را وارد کنید.\n\n"
        "مثال:\n"
        "`12`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
    )


async def receive_receipt_id(
    update,
    context,
):
    if not context.user_data.get(
        "receipt_id"
    ):
        return

    try:
        request_id = int(
            update.message.text.strip()
        )
    except ValueError:
        await update.message.reply_text(
            "❌ شناسه نامعتبر است."
        )
        return

    conn = db()

    request = conn.execute("""
        SELECT id, status
        FROM requests
        WHERE id = ?
        AND user_id = ?
    """, (
        request_id,
        update.effective_user.id,
    )).fetchone()

    conn.close()

    if not request:
        await update.message.reply_text(
            "❌ درخواست پیدا نشد."
        )
        return

    if request["status"] != "reserved":
        await update.message.reply_text(
            "❌ این درخواست در وضعیت ارسال فیش نیست."
        )
        return

    clear_state(context)

    context.user_data[
        "receipt_request"
    ] = request_id

    await update.message.reply_text(
        "📎 *حالا فیش پرداخت را ارسال کنید.*\n\n"
        "عکس یا فایل قابل قبول است.",
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

    conn.execute("""
        UPDATE requests
        SET receipt_file_id = ?,
            receipt_type = ?,
            updated_at = ?
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

    await update.message.reply_text(
        "✅ *فیش دریافت شد.*\n\n"
        f"🆔 درخواست: `{request_id}`\n\n"
        "⏳ برای ادمین ارسال شد.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=user_menu(),
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تأیید",
                callback_data=f"ok_{request_id}",
            ),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"no_{request_id}",
            ),
        ],
    ])

    admin_text = (
        "🧾 *فیش جدید*\n\n"
        f"🆔 درخواست: `{request_id}`\n"
        f"👤 {user.first_name or '-'}\n"
        f"🔹 @{user.username or '-'}\n"
        f"💰 `{fmt_amount(request['amount'])}` تومان"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                admin_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )

            if receipt_type == "photo":
                await context.bot.send_photo(
                    admin_id,
                    file_id,
                    caption=f"🧾 فیش #{request_id}",
                )
            else:
                await context.bot.send_document(
                    admin_id,
                    file_id,
                    caption=f"🧾 فیش #{request_id}",
                )

        except Exception:
            pass


# =========================================================
# APPROVE
# =========================================================

async def approve(update, context, request_id):
    query = update.callback_query

    # پاسخ فوری
    await query.answer("✅ تأیید شد")

    if not is_admin(query.from_user.id):
        return

    conn = db()

    request = conn.execute("""
        SELECT *
        FROM requests
        WHERE id = ?
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
            updated_at = ?
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

    # حذف دکمه‌های قبلی
    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    # اطلاع کاربر
    try:
        await context.bot.send_message(
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

    if not is_admin(query.from_user.id):
        return

    conn = db()

    request = conn.execute("""
        SELECT *
        FROM requests
        WHERE id = ?
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
            updated_at = ?
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
        await context.bot.send_message(
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
            active
        FROM payment_targets
        ORDER BY id ASC
    """).fetchall()

    conn.close()

    if not rows:
        text = (
            "📊 *وضعیت حساب‌ها*\n\n"
            "هیچ حسابی وجود ندارد."
        )

    else:
        parts = [
            "📊 *وضعیت حساب‌ها*\n"
        ]

        for row in rows:
            remaining = available_amount(row)

            status = (
                "🟢 فعال"
                if row["active"]
                else "🔴 بسته"
            )

            parts.append(
                f"🆔 `{row['id']}` | {status}\n"
                f"👤 {row['owner_name']}\n"
                f"🏦 `{row['account_number']}`\n"
                f"💳 سقف: `{fmt_amount(row['capacity'])}`\n"
                f"🟡 رزرو: `{fmt_amount(row['reserved_amount'])}`\n"
                f"🟢 پرداخت: `{fmt_amount(row['paid_amount'])}`\n"
                f"📊 باقی: `{fmt_amount(remaining)}`\n"
                "━━━━━━━━━━━━━━"
            )

        text = "\n".join(parts)

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=back_button(),
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
        ORDER BY id ASC
    """).fetchall()

    conn.close()

    if not rows:
        await query.edit_message_text(
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
            ),
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ بازگشت",
            callback_data="main",
        ),
    ])

    await query.edit_message_text(
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
        await query.edit_message_text(
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
        f"👤 {row['first_name'] or '-'}\n"
        f"🔹 @{row['username'] or '-'}\n"
        f"🔢 `{row['user_id']}`\n"
        f"💰 `{fmt_amount(row['amount'])}` تومان\n"
        f"{receipt}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تأیید",
                callback_data=f"ok_{request_id}",
            ),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"no_{request_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "⬅️ برگشت",
                callback_data="a_pending",
            ),
        ],
    ])

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


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

    await query.edit_message_text(
        "➕ *افزودن حساب*\n\n"
        "فرمت:\n"
        "`نام|شماره‌حساب|سقف`\n\n"
        "مثال:\n"
        "`علی امینی|6037991234567890|100000000`",
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

    if len(parts) != 3:
        await update.message.reply_text(
            "❌ فرمت اشتباه است.\n\n"
            "`نام|شماره‌حساب|سقف`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    owner = parts[0].strip()
    account = parts[1].strip()
    capacity = parse_amount(parts[2])

    if not owner:
        await update.message.reply_text(
            "❌ نام خالی است."
        )
        return

    if not account:
        await update.message.reply_text(
            "❌ شماره حساب خالی است."
        )
        return

    if capacity is None:
        await update.message.reply_text(
            "❌ سقف نامعتبر است."
        )
        return

    conn = db()

    cursor = conn.execute("""
        INSERT INTO payment_targets (
            owner_name,
            account_number,
            capacity,
            reserved_amount,
            paid_amount,
            active,
            created_at
        )
        VALUES (
            ?, ?, ?, 0, 0, 1, ?
        )
    """, (
        owner,
        account,
        capacity,
        now_iso(),
    ))

    target_id = cursor.lastrowid

    # درخواست‌های منتظر
    waiting = conn.execute("""
        SELECT *
        FROM requests
        WHERE status = 'waiting'
        ORDER BY id ASC
    """).fetchall()

    assigned = []

    for request in waiting:

        target = conn.execute("""
            SELECT *
            FROM payment_targets
            WHERE id = ?
        """, (
            target_id,
        )).fetchone()

        if available_amount(target) < request["amount"]:
            continue

        conn.execute("""
            UPDATE requests
            SET target_id = ?,
                status = 'reserved',
                updated_at = ?
            WHERE id = ?
        """, (
            target_id,
            now_iso(),
            request["id"],
        ))

        conn.execute("""
            UPDATE payment_targets
            SET reserved_amount =
                reserved_amount + ?
            WHERE id = ?
        """, (
            request["amount"],
            target_id,
        ))

        assigned.append(dict(request))

    conn.commit()
    conn.close()

    clear_state(context)

    await update.message.reply_text(
        "✅ *حساب اضافه شد*\n\n"
        f"🆔 `{target_id}`\n"
        f"👤 `{owner}`\n"
        f"🏦 `{account}`\n"
        f"💳 `{fmt_amount(capacity)}` تومان\n\n"
        f"🔄 درخواست اختصاص‌یافته: `{len(assigned)}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )

    # اطلاع کاربران
    for request in assigned:
        try:
            await context.bot.send_message(
                request["user_id"],
                "🎉 *حساب برای شما آماده شد!*\n\n"
                f"🆔 درخواست: `{request['id']}`\n"
                f"👤 صاحب حساب: `{owner}`\n"
                f"🏦 شماره حساب:\n"
                f"`{account}`\n\n"
                f"💰 مبلغ:\n"
                f"`{fmt_amount(request['amount'])}` تومان\n\n"
                "پس از پرداخت، فیش را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🧾 ارسال فیش",
                            callback_data=f"r_{request['id']}",
                        ),
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
        await query.edit_message_text(
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
            ),
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ بازگشت",
            callback_data="main",
        ),
    ])

    await query.edit_message_text(
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
        SELECT id, amount, status
        FROM requests
        WHERE target_id = ?
        ORDER BY id DESC
        LIMIT 30
    """, (
        target_id,
    )).fetchall()

    conn.close()

    if not target:
        await query.edit_message_text(
            "❌ حساب پیدا نشد.",
            reply_markup=back_button(),
        )
        return

    status_names = {
        "reserved": "🟡 رزرو",
        "paid": "🟢 پرداخت",
        "rejected": "🔴 رد",
        "waiting": "⏳ انتظار",
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
                f"💰 `{fmt_amount(row['amount'])}` | "
                f"{status_names.get(row['status'], row['status'])}"
            )

    await query.edit_message_text(
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

        await query.edit_message_text(
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
    # REQUESTS
    # -----------------------------------------------------

    if data == "u_requests":
        await my_requests(
            update,
            context,
        )
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

    # -----------------------------------------------------
    # HELP
    # -----------------------------------------------------

    if data == "u_help":

        await query.edit_message_text(
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
    # ADMIN PENDING
    # -----------------------------------------------------

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
    # RECEIPT REQUEST
    # -----------------------------------------------------

    if data.startswith("r_"):

        try:
            request_id = int(
                data[2:]
            )
        except ValueError:
            return

        context.user_data[
            "receipt_request"
        ] = request_id

        await query.edit_message_text(
            f"🧾 *فیش درخواست `{request_id}`*\n\n"
            "حالا عکس یا فایل فیش را ارسال کنید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_button(),
        )
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
        await update.message.reply_text(
            "مثال:\n"
            "`/getaccount 50000000`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    amount = parse_amount(
        context.args[0]
    )

    if amount is None:
        await update.message.reply_text(
            "❌ مبلغ نامعتبر است."
        )
        return

    await create_request(
        update,
        context,
        amount,
    )


# =========================================================
# OLD COMMAND: RECEIPT
# =========================================================

async def command_receipt(
    update,
    context,
):
    if not context.args:
        await update.message.reply_text(
            "مثال:\n"
            "`/receipt 12`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        request_id = int(
            context.args[0]
        )
    except ValueError:
        await update.message.reply_text(
            "❌ شناسه نامعتبر است."
        )
        return

    conn = db()

    request = conn.execute("""
        SELECT id, status
        FROM requests
        WHERE id = ?
        AND user_id = ?
    """, (
        request_id,
        update.effective_user.id,
    )).fetchone()

    conn.close()

    if not request:
        await update.message.reply_text(
            "❌ درخواست پیدا نشد."
        )
        return

    if request["status"] != "reserved":
        await update.message.reply_text(
            "❌ این درخواست قابل ارسال فیش نیست."
        )
        return

    clear_state(context)

    context.user_data[
        "receipt_request"
    ] = request_id

    await update.message.reply_text(
        "📎 حالا فیش را ارسال کنید."
    )


# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(
    update,
    context,
):
    if context.user_data.get("amount"):
        await receive_amount(
            update,
            context,
        )
        return

    if context.user_data.get("receipt_id"):
        await receive_receipt_id(
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
        raise RuntimeError("BOT_TOKEN در فایل .env تنظیم نشده است.")

    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS در فایل .env تنظیم نشده است.")

    init_db()

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

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(BOT_REQUEST)
        .get_updates_request(GET_UPDATES_REQUEST)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("getaccount", get_account_menu))
    application.add_handler(CommandHandler("receipt", receipt_menu))

    application.add_handler(
        CallbackQueryHandler(callback_router)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.ALL,
            receive_receipt
        )
    )

    application.add_error_handler(error_handler)

    print("ربات در حال اجراست...")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
# =========================================================

if __name__ == "__main__":
    main()
