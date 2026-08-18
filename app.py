from flask import Flask, request, jsonify
import requests
import os
import psycopg
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)

# =========================================================
# Environment Variables
# =========================================================

TOKEN = os.getenv("BALE_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if TOKEN:
    BALE_API = f"https://tapi.bale.ai/bot{TOKEN}/sendMessage"
else:
    BALE_API = None

# ساعت ایران
IRAN_TZ = ZoneInfo("Asia/Tehran")

# مدت یادآوری تأخیری
SNOOZE_MINUTES = 5

# =========================================================
# Database Connection
# =========================================================

def get_db_connection():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set"
        )

    return psycopg.connect(
        DATABASE_URL
    )


# =========================================================
# Initialize Database
# =========================================================

def init_database():

    if not DATABASE_URL:
        print(
            "ERROR: DATABASE_URL is not set"
        )
        return

    try:

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                # -------------------------------------------------
                # Users
                # -------------------------------------------------

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,

                        bale_user_id BIGINT UNIQUE NOT NULL,

                        first_name TEXT,

                        username TEXT,

                        display_name TEXT,

                        created_at TIMESTAMP
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                cur.execute("""
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS display_name TEXT;
                """)

                # -------------------------------------------------
                # Medications
                # -------------------------------------------------

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS medications (
                        id SERIAL PRIMARY KEY,

                        user_id INTEGER NOT NULL
                            REFERENCES users(id)
                            ON DELETE CASCADE,

                        name TEXT NOT NULL,

                        times_per_day INTEGER NOT NULL,

                        created_at TIMESTAMP
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # -------------------------------------------------
                # Medication schedules
                # -------------------------------------------------

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS medication_schedules (
                        id SERIAL PRIMARY KEY,

                        medication_id INTEGER NOT NULL
                            REFERENCES medications(id)
                            ON DELETE CASCADE,

                        dose_number INTEGER NOT NULL,

                        time TEXT NOT NULL
                    );
                """)

                # -------------------------------------------------
                # User sessions
                # -------------------------------------------------

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_sessions (
                        id SERIAL PRIMARY KEY,

                        user_id INTEGER UNIQUE NOT NULL
                            REFERENCES users(id)
                            ON DELETE CASCADE,

                        state TEXT NOT NULL,

                        data TEXT,

                        updated_at TIMESTAMP
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # -------------------------------------------------
                # Reminder occurrences
                #
                # وضعیت هر نوبت دارو در هر روز
                #
                # pending
                # sent
                # snoozed
                # taken
                # not_taken
                #
                # این جدول جلوی ارسال تکراری را می‌گیرد.
                # -------------------------------------------------

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reminder_occurrences (

                        id SERIAL PRIMARY KEY,

                        medication_schedule_id INTEGER NOT NULL
                            REFERENCES medication_schedules(id)
                            ON DELETE CASCADE,

                        reminder_date DATE NOT NULL,

                        scheduled_time TEXT NOT NULL,

                        status TEXT NOT NULL DEFAULT 'pending',

                        sent_at TIMESTAMP,

                        taken_at TIMESTAMP,

                        not_taken_at TIMESTAMP,

                        snooze_until TIMESTAMP,

                        created_at TIMESTAMP
                            DEFAULT CURRENT_TIMESTAMP,

                        UNIQUE (
                            medication_schedule_id,
                            reminder_date
                        )
                    );
                """)

                # -------------------------------------------------
                # اگر جدول reminder_logs نسخه قبلی وجود داشته باشد
                # حذف نمی‌شود تا اطلاعات قبلی از بین نرود.
                # -------------------------------------------------

            conn.commit()

        print(
            "Database initialized successfully."
        )

    except Exception as e:

        print(
            "Database initialization error:",
            repr(e)
        )


# =========================================================
# Bale API
# =========================================================

def send_message(
    chat_id,
    text,
    buttons=None
):

    if not BALE_API:

        print(
            "ERROR: BALE_BOT_TOKEN is not set"
        )

        return None

    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if buttons:

        payload["reply_markup"] = {
            "keyboard": buttons,
            "resize_keyboard": True
        }

    try:

        response = requests.post(
            BALE_API,
            json=payload,
            timeout=15
        )

        print(
            "Bale response:",
            response.status_code,
            response.text
        )

        return response

    except Exception as e:

        print(
            "Bale send error:",
            repr(e)
        )

        return None


# =========================================================
# User
# =========================================================

def get_or_create_user(user):

    bale_user_id = user.get("id")

    if not bale_user_id:
        return None

    first_name = user.get(
        "first_name"
    )

    username = user.get(
        "username"
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO users (
                    bale_user_id,
                    first_name,
                    username
                )

                VALUES (%s, %s, %s)

                ON CONFLICT (bale_user_id)

                DO UPDATE SET
                    first_name =
                        EXCLUDED.first_name,

                    username =
                        EXCLUDED.username

                RETURNING
                    id,
                    display_name;
            """, (
                bale_user_id,
                first_name,
                username
            ))

            row = cur.fetchone()

        conn.commit()

    return {
        "id": row[0],
        "display_name": row[1]
    }


# =========================================================
# Session
# =========================================================

def set_session(
    user_id,
    state,
    data=None
):

    data_json = json.dumps(
        data or {},
        ensure_ascii=False
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO user_sessions (
                    user_id,
                    state,
                    data,
                    updated_at
                )

                VALUES (
                    %s,
                    %s,
                    %s,
                    CURRENT_TIMESTAMP
                )

                ON CONFLICT (user_id)

                DO UPDATE SET
                    state =
                        EXCLUDED.state,

                    data =
                        EXCLUDED.data,

                    updated_at =
                        CURRENT_TIMESTAMP;
            """, (
                user_id,
                state,
                data_json
            ))

        conn.commit()


def get_session(user_id):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    state,
                    data

                FROM user_sessions

                WHERE user_id = %s;
            """, (
                user_id,
            ))

            row = cur.fetchone()

    if not row:
        return None

    try:

        data = (
            json.loads(row[1])
            if row[1]
            else {}
        )

    except Exception:

        data = {}

    return {
        "state": row[0],
        "data": data
    }


def clear_session(user_id):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                DELETE FROM user_sessions

                WHERE user_id = %s;
            """, (
                user_id,
            ))

        conn.commit()


# =========================================================
# Display Name
# =========================================================

def save_display_name(
    user_id,
    display_name
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE users

                SET display_name = %s

                WHERE id = %s;
            """, (
                display_name,
                user_id
            ))

        conn.commit()


# =========================================================
# Save Medication
# =========================================================

def save_medication(
    user_id,
    medication_name,
    times_per_day,
    times
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO medications (
                    user_id,
                    name,
                    times_per_day
                )

                VALUES (
                    %s,
                    %s,
                    %s
                )

                RETURNING id;
            """, (
                user_id,
                medication_name,
                times_per_day
            ))

            medication_id = (
                cur.fetchone()[0]
            )

            for index, time in enumerate(
                times,
                start=1
            ):

                cur.execute("""
                    INSERT INTO medication_schedules (
                        medication_id,
                        dose_number,
                        time
                    )

                    VALUES (
                        %s,
                        %s,
                        %s
                    );
                """, (
                    medication_id,
                    index,
                    time
                ))

        conn.commit()

    return medication_id


# =========================================================
# Time Validation
# =========================================================

def is_valid_time(time_text):

    pattern = (
        r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )

    return bool(
        re.match(
            pattern,
            time_text
        )
    )


# =========================================================
# Main Menu
# =========================================================

def main_menu(
    chat_id,
    name
):

    send_message(

        chat_id,

        f"خیلی خوشحالم که با من آشنا شدی "
        f"{name} جان 🌱\n\n"

        "من «مهرور» هستم؛ "
        "سامانه یادآوری داروهای شما 💚\n\n"

        "از منوی زیر می‌تونی داروهات رو مدیریت کنی.",

        buttons=[
            ["➕ افزودن دارو"],
            ["💊 داروهای من"],
            ["❌ لغو"]
        ]
    )


# =========================================================
# Start Conversation
# =========================================================

def start_conversation(
    chat_id,
    db_user
):

    display_name = db_user.get(
        "display_name"
    )

    if display_name:

        main_menu(
            chat_id,
            display_name
        )

        set_session(
            db_user["id"],
            "MAIN_MENU",
            {}
        )

    else:

        send_message(

            chat_id,

            "سلام 🌱\n\n"

            "من «مهرور» هستم؛ "
            "سامانه یادآوری داروهای شما 💚\n\n"

            "خوشحال می‌شم بدونم "
            "با چه اسمی صداتون بزنم؟"
        )

        set_session(
            db_user["id"],
            "ASK_NAME",
            {}
        )


# =========================================================
# List Medications
# =========================================================

def show_medications(
    chat_id,
    user_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    name,
                    times_per_day

                FROM medications

                WHERE user_id = %s

                ORDER BY id;
            """, (
                user_id,
            ))

            medications = cur.fetchall()

            if not medications:

                send_message(

                    chat_id,

                    "هنوز هیچ دارویی ثبت نکردی 💊\n\n"

                    "برای شروع، روی "
                    "«➕ افزودن دارو» بزن.",

                    buttons=[
                        ["➕ افزودن دارو"],
                        ["↩️ منوی اصلی"]
                    ]
                )

                return

            text = (
                "💊 داروهای ثبت‌شده شما:\n\n"
            )

            for index, medication in enumerate(
                medications,
                start=1
            ):

                medication_id = medication[0]
                name = medication[1]
                times_per_day = medication[2]

                text += (
                    f"{index}. 💊 {name}\n"
                    f"   🔢 تعداد مصرف روزانه: "
                    f"{times_per_day} بار\n"
                )

                cur.execute("""
                    SELECT time

                    FROM medication_schedules

                    WHERE medication_id = %s

                    ORDER BY dose_number;
                """, (
                    medication_id,
                ))

                schedules = cur.fetchall()

                for schedule in schedules:

                    text += (
                        f"   ⏰ {schedule[0]}\n"
                    )

                text += "\n"

    send_message(

        chat_id,

        text,

        buttons=[
            ["➕ افزودن دارو"],
            ["↩️ منوی اصلی"]
        ]
    )


# =========================================================
# Cancel
# =========================================================

def cancel_conversation(
    chat_id,
    user_id
):

    clear_session(
        user_id
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT display_name

                FROM users

                WHERE id = %s;
            """, (
                user_id,
            ))

            row = cur.fetchone()

    name = (
        row[0]
        if row and row[0]
        else "دوست من"
    )

    send_message(
        chat_id,
        "عملیات لغو شد. 🌱"
    )

    main_menu(
        chat_id,
        name
    )

    set_session(
        user_id,
        "MAIN_MENU",
        {}
    )


# =========================================================
# Add Medication
# =========================================================

def start_add_medication(
    chat_id,
    user_id
):

    set_session(
        user_id,
        "ASK_MEDICATION_NAME",
        {}
    )

    send_message(

        chat_id,

        "💊 خیلی خوب.\n\n"

        "اسم دارویی که می‌خوای "
        "ثبت کنی رو بنویس:"
    )


# =========================================================
# Confirmation
# =========================================================

def show_confirmation(
    chat_id,
    user_id,
    data
):

    name = data.get(
        "medication_name"
    )

    times_per_day = data.get(
        "times_per_day"
    )

    times = data.get(
        "times",
        []
    )

    text = (
        "📋 اطلاعات دارو:\n\n"

        f"💊 نام دارو: {name}\n"

        f"🔢 تعداد مصرف در روز: "
        f"{times_per_day} بار\n\n"

        "⏰ ساعت‌های مصرف:\n"
    )

    for index, time in enumerate(
        times,
        start=1
    ):

        text += (
            f"{index}. {time}\n"
        )

    text += (
        "\nآیا اطلاعات بالا درست است؟"
    )

    set_session(
        user_id,
        "CONFIRM_MEDICATION",
        data
    )

    send_message(

        chat_id,

        text,

        buttons=[
            ["✅ ثبت دارو"],
            ["🔄 اصلاح اطلاعات"],
            ["❌ لغو"]
        ]
    )


# =========================================================
# Reminder Buttons
# =========================================================

REMINDER_TAKEN = "✅ مصرف کردم"

REMINDER_SNOOZE = (
    "⏰ ۵ دقیقه بعد یادآوری کن"
)

REMINDER_NOT_TAKEN = (
    "❌ مصرف نکردم"
)


# =========================================================
# Get Active Reminder
# =========================================================

def get_active_reminder(
    user_id
):

    today = datetime.now(
        IRAN_TZ
    ).date()

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    ro.id,
                    ro.medication_schedule_id,
                    ro.status,
                    ro.scheduled_time,
                    ro.snooze_until,
                    m.name,
                    ms.dose_number

                FROM reminder_occurrences ro

                JOIN medication_schedules ms
                    ON ms.id =
                       ro.medication_schedule_id

                JOIN medications m
                    ON m.id =
                       ms.medication_id

                WHERE m.user_id = %s

                AND ro.reminder_date = %s

                AND ro.status IN (
                    'sent',
                    'snoozed'
                )

                ORDER BY ro.id DESC

                LIMIT 1;
            """, (
                user_id,
                today
            ))

            row = cur.fetchone()

    return row


# =========================================================
# Mark Reminder Taken
# =========================================================

def mark_reminder_taken(
    reminder_id
):

    now = datetime.now(
        IRAN_TZ
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE reminder_occurrences

                SET
                    status = 'taken',
                    taken_at = %s,
                    snooze_until = NULL

                WHERE id = %s

                AND status IN (
                    'sent',
                    'snoozed'
                );
            """, (
                now,
                reminder_id
            ))

        conn.commit()


# =========================================================
# Mark Reminder Not Taken
# =========================================================

def mark_reminder_not_taken(
    reminder_id
):

    now = datetime.now(
        IRAN_TZ
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE reminder_occurrences

                SET
                    status = 'not_taken',
                    not_taken_at = %s,
                    snooze_until = NULL

                WHERE id = %s

                AND status IN (
                    'sent',
                    'snoozed'
                );
            """, (
                now,
                reminder_id
            ))

        conn.commit()


# =========================================================
# Snooze Reminder
# =========================================================

def snooze_reminder(
    reminder_id
):

    snooze_until = (
        datetime.now(IRAN_TZ)
        + timedelta(
            minutes=SNOOZE_MINUTES
        )
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE reminder_occurrences

                SET
                    status = 'snoozed',
                    snooze_until = %s

                WHERE id = %s

                AND status IN (
                    'sent',
                    'snoozed'
                );
            """, (
                snooze_until,
                reminder_id
            ))

        conn.commit()

    return snooze_until


# =========================================================
# Handle Reminder Action
# =========================================================

def handle_reminder_action(
    chat_id,
    user_id,
    text
):

    reminder = get_active_reminder(
        user_id
    )

    if not reminder:

        return False

    reminder_id = reminder[0]
    medication_name = reminder[5]

    # -----------------------------------------------------
    # مصرف کردم
    # -----------------------------------------------------

    if text == REMINDER_TAKEN:

        mark_reminder_taken(
            reminder_id
        )

        send_message(

            chat_id,

            f"✅ ثبت شد.\n\n"
            f"مصرف «{medication_name}» "
            f"انجام‌شده ثبت شد. 💚\n\n"
            "برای این نوبت دیگر یادآوری نمی‌کنم. 🌱",

            buttons=[
                ["➕ افزودن دارو"],
                ["💊 داروهای من"],
                ["↩️ منوی اصلی"]
            ]
        )

        return True

    # -----------------------------------------------------
    # مصرف نکردم
    # -----------------------------------------------------

    if text == REMINDER_NOT_TAKEN:

        mark_reminder_not_taken(
            reminder_id
        )

        send_message(

            chat_id,

            f"ثبت شد. ❌\n\n"
            f"نوبت «{medication_name}» "
            "مصرف‌نشده ثبت شد.\n\n"
            "برای این نوبت دیگر یادآوری نمی‌کنم. 🌱",

            buttons=[
                ["➕ افزودن دارو"],
                ["💊 داروهای من"],
                ["↩️ منوی اصلی"]
            ]
        )

        return True

    # -----------------------------------------------------
    # یادآوری ۵ دقیقه بعد
    # -----------------------------------------------------

    if text == REMINDER_SNOOZE:

        snooze_until = snooze_reminder(
            reminder_id
        )

        time_text = snooze_until.strftime(
            "%H:%M"
        )

        send_message(

            chat_id,

            f"باشه 🌱\n\n"
            f"یادآوری «{medication_name}» "
            f"برای ساعت {time_text} تنظیم شد.\n\n"
            "۵ دقیقه دیگه دوباره بهت یادآوری می‌کنم. ⏰",

            buttons=[
                [REMINDER_TAKEN],
                [REMINDER_NOT_TAKEN]
            ]
        )

        return True

    return False


# =========================================================
# Create Today's Reminder Occurrences
# =========================================================

def create_due_occurrences(
    cur,
    today,
    now
):

    # -------------------------------------------------
    # only remined items with 10 minuts delay or less
    # -------------------------------------------------

    window_start = now - timedelta(
        minutes=10
    )

    cur.execute("""
        SELECT
            ms.id,
            ms.time

        FROM medication_schedules ms

        ORDER BY ms.time;
    """)

    schedules = cur.fetchall()

    created = 0

    for schedule in schedules:

        schedule_id = schedule[0]
        scheduled_time = schedule[1]

        # -------------------------------------------------
        # تبدیل ساعت دارو مثل 10:00 به datetime امروز
        # -------------------------------------------------

        try:

            scheduled_datetime = datetime.combine(
                today,
                datetime.strptime(
                    scheduled_time,
                    "%H:%M"
                ).time()
            )

            scheduled_datetime = scheduled_datetime.replace(
                tzinfo=IRAN_TZ
            )

        except Exception as e:

            print(
                "Invalid medication schedule time:",
                scheduled_time,
                repr(e)
            )

            continue

        # -------------------------------------------------
        # only times with 10 minuts in window
        # -------------------------------------------------

        if not (
            window_start
            <= scheduled_datetime
            <= now
        ):

            continue

        # -------------------------------------------------
        # create occurrence
        # -------------------------------------------------

        cur.execute("""
            INSERT INTO reminder_occurrences (
                medication_schedule_id,
                reminder_date,
                scheduled_time,
                status
            )

            VALUES (
                %s,
                %s,
                %s,
                'pending'
            )

            ON CONFLICT (
                medication_schedule_id,
                reminder_date
            )

            DO NOTHING;
        """, (
            schedule_id,
            today,
            scheduled_time
        ))

        if cur.rowcount > 0:

            created += 1

    return created

# =========================================================
# REMINDER SYSTEM
# =========================================================

@app.route(
    "/check-reminders",
    methods=["GET"]
)
def check_reminders():

    print(
        "Reminder check started."
    )

    now = datetime.now(
        IRAN_TZ
    )

    current_time = now.strftime(
        "%H:%M"
    )

    today = now.date()

    sent_count = 0
    error_count = 0
    snooze_count = 0

    try:

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                # -------------------------------------------------
                # first create tinmes that reach times
                #
                # -------------------------------------------------

                created = create_due_occurrences(
                    cur,
                    today,
                    now
                )

                print(
                    f"Created {created} "
                    f"due occurrence(s)."
                )

                # -------------------------------------------------
                # # نوبت‌های معمولی که هنوز ارسال نشده‌اند
                # -------------------------------------------------

                cur.execute("""
                    SELECT
                        ro.id,
                        ro.medication_schedule_id,
                        ro.scheduled_time,
                        m.name,
                        u.bale_user_id,
                        u.display_name,
                        ms.dose_number

                    FROM reminder_occurrences ro

                    JOIN medication_schedules ms
                        ON ms.id =
                           ro.medication_schedule_id

                    JOIN medications m
                        ON m.id =
                           ms.medication_id

                    JOIN users u
                        ON u.id =
                           m.user_id

                    WHERE ro.reminder_date = %s

                    AND ro.status = 'pending'

                    ORDER BY
                        ro.scheduled_time,
                        ro.id;
                """, (
                    today,
                ))

                reminders = cur.fetchall()

                # -------------------------------------------------
                # فقط pendingهایی که هنوز داخل پنجره ۱۰ دقیقه‌ای
                # هستند اجازه ارسال دارند.
                # -------------------------------------------------
                
                window_start = now - timedelta(
                    minutes=10
                )
                
                valid_reminders = []
                
                for reminder in reminders:
                
                    medication_time = reminder[2]
                
                    try:
                
                        scheduled_datetime = datetime.combine(
                            today,
                            datetime.strptime(
                                medication_time,
                                "%H:%M"
                            ).time()
                        )
                
                        scheduled_datetime = scheduled_datetime.replace(
                            tzinfo=IRAN_TZ
                        )
                
                    except Exception as e:
                
                        print(
                            "Invalid reminder time:",
                            medication_time,
                            repr(e)
                        )
                
                        continue
                
                    if (
                        window_start
                        <= scheduled_datetime
                        <= now
                    ):
                
                        valid_reminders.append(
                            reminder
                        )
                
                reminders = valid_reminders
                
                print(
                    f"Found {len(reminders)} "
                    f"valid pending reminder(s)."
                )

                for reminder in reminders:

                    reminder_id = reminder[0]

                    medication_time = reminder[2]

                    medication_name = reminder[3]

                    bale_user_id = reminder[4]

                    display_name = reminder[5]

                    dose_number = reminder[6]

                    if display_name:

                        greeting = (
                            f"{display_name} جان 🌱"
                        )

                    else:

                        greeting = (
                            "دوست عزیز 🌱"
                        )

                    message = (
                        "⏰ یادآوری مصرف دارو\n\n"

                        f"{greeting}\n\n"

                        f"💊 زمان مصرف "
                        f"«{medication_name}» "
                        "رسیده است.\n\n"

                        f"🕐 ساعت: "
                        f"{medication_time}\n"

                        f"💊 نوبت مصرف: "
                        f"{dose_number}\n\n"

                        "اگر مصرف کردی، ثبتش کن. "
                        "اگر الان نمی‌تونی، "
                        "۵ دقیقه بعد دوباره یادآوری می‌کنم."
                    )

                    response = send_message(

                        bale_user_id,

                        message,

                        buttons=[
                            [REMINDER_TAKEN],
                            [REMINDER_SNOOZE]
                        ]
                    )

                    if response and response.ok:

                        cur.execute("""
                            UPDATE reminder_occurrences

                            SET
                                status = 'sent',
                                sent_at = %s

                            WHERE id = %s

                            AND status = 'pending';
                        """, (
                            now,
                            reminder_id
                        ))

                        sent_count += 1

                        print(
                            "Reminder sent:",
                            medication_name,
                            medication_time,
                            bale_user_id
                        )

                    else:

                        error_count += 1

                        print(
                            "Reminder failed:",
                            medication_name,
                            medication_time,
                            bale_user_id
                        )

                # -------------------------------------------------
                # یادآوری‌های ۵ دقیقه‌ای
                # -------------------------------------------------

                cur.execute("""
                    SELECT
                        ro.id,
                        ro.scheduled_time,
                        ro.snooze_until,
                        m.name,
                        u.bale_user_id,
                        u.display_name,
                        ms.dose_number

                    FROM reminder_occurrences ro

                    JOIN medication_schedules ms
                        ON ms.id =
                           ro.medication_schedule_id

                    JOIN medications m
                        ON m.id =
                           ms.medication_id

                    JOIN users u
                        ON u.id =
                           m.user_id

                    WHERE ro.reminder_date = %s

                    AND ro.status = 'snoozed'

                    AND ro.snooze_until <= %s

                    ORDER BY
                        ro.snooze_until,
                        ro.id;
                """, (
                    today,
                    now
                ))

                snoozed_reminders = cur.fetchall()

                print(
                    f"Found {len(snoozed_reminders)} "
                    f"snoozed reminder(s)."
                )

                for reminder in snoozed_reminders:

                    reminder_id = reminder[0]

                    medication_time = reminder[1]

                    medication_name = reminder[3]

                    bale_user_id = reminder[4]

                    display_name = reminder[5]

                    dose_number = reminder[6]

                    if display_name:

                        greeting = (
                            f"{display_name} جان 🌱"
                        )

                    else:

                        greeting = (
                            "دوست عزیز 🌱"
                        )

                    message = (
                        "⏰ یادآوری مجدد مصرف دارو\n\n"

                        f"{greeting}\n\n"

                        f"💊 داروی "
                        f"«{medication_name}» "
                        "هنوز در انتظار ثبت وضعیت است.\n\n"

                        "آیا مصرفش کردی؟"
                    )

                    response = send_message(

                        bale_user_id,

                        message,

                        buttons=[
                            [REMINDER_TAKEN],
                            [REMINDER_NOT_TAKEN]
                        ]
                    )

                    if response and response.ok:

                        cur.execute("""
                            UPDATE reminder_occurrences

                            SET
                                status = 'sent',
                                sent_at = %s,
                                snooze_until = NULL

                            WHERE id = %s

                            AND status = 'snoozed';
                        """, (
                            now,
                            reminder_id
                        ))

                        snooze_count += 1

                        print(
                            "Snoozed reminder sent:",
                            medication_name,
                            bale_user_id
                        )

                    else:

                        error_count += 1

                        print(
                            "Snoozed reminder failed:",
                            medication_name,
                            bale_user_id
                        )

            conn.commit()

        print(
            "Reminder check finished. "
            f"Sent={sent_count}, "
            f"Snoozed={snooze_count}, "
            f"Errors={error_count}"
        )

        return jsonify({

            "status": "ok",

            "current_time": current_time,

            "sent": sent_count,

            "snoozed_sent": snooze_count,

            "errors": error_count
        })

    except Exception as e:

        print(
            "Reminder system error:",
            repr(e)
        )

        return jsonify({

            "status": "error",

            "error": str(e)
        }), 500


# =========================================================
# Message Handler
# =========================================================

@app.route(
    "/message",
    methods=["POST"]
)
def receive_message():

    data = request.json

    print(
        "Received:",
        data
    )

    try:

        message = data.get(
            "message",
            {}
        )

        chat = message.get(
            "chat",
            {}
        )

        user = message.get(
            "from",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        text = message.get(
            "text",
            ""
        ).strip()

        # -------------------------------------------------
        # User
        # -------------------------------------------------

        db_user = get_or_create_user(
            user
        )

        if not db_user:

            return jsonify({
                "status": "ok"
            })

        user_id = db_user["id"]

        # -------------------------------------------------
        # Reminder buttons
        #
        # این قسمت قبل از Session بررسی می‌شود.
        # بنابراین کاربر هر زمانی روی دکمه یادآوری
        # کلیک کند، وضعیت نوبت ثبت می‌شود.
        # -------------------------------------------------

        if text in [
            REMINDER_TAKEN,
            REMINDER_SNOOZE,
            REMINDER_NOT_TAKEN
        ]:

            handled = handle_reminder_action(
                chat_id,
                user_id,
                text
            )

            if handled:

                return jsonify({
                    "status": "ok"
                })

        # -------------------------------------------------
        # Cancel
        # -------------------------------------------------

        if text in [
            "❌ لغو",
            "/cancel",
            "لغو"
        ]:

            cancel_conversation(
                chat_id,
                user_id
            )

            return jsonify({
                "status": "ok"
            })

        # -------------------------------------------------
        # Start
        # -------------------------------------------------

        if text in [
            "/start",
            "شروع",
            "سلام"
        ]:

            start_conversation(
                chat_id,
                db_user
            )

            return jsonify({
                "status": "ok"
            })

        # -------------------------------------------------
        # Session
        # -------------------------------------------------

        session = get_session(
            user_id
        )

        state = (
            session["state"]
            if session
            else "MAIN_MENU"
        )

        session_data = (
            session["data"]
            if session
            else {}
        )

        # =================================================
        # ASK NAME
        # =================================================

        if state == "ASK_NAME":

            display_name = text

            if not display_name:

                send_message(
                    chat_id,
                    "لطفاً اسمتون رو وارد کنید 🙂"
                )

                return jsonify({
                    "status": "ok"
                })

            save_display_name(
                user_id,
                display_name
            )

            set_session(
                user_id,
                "MAIN_MENU",
                {}
            )

            send_message(

                chat_id,

                f"{display_name} جان، "
                "خیلی خوشحالم که آشنات شدم 🌱\n\n"

                "حالا می‌تونیم داروهات رو ثبت کنیم.",

                buttons=[
                    ["➕ افزودن دارو"],
                    ["💊 داروهای من"]
                ]
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # MAIN MENU
        # =================================================

        if state == "MAIN_MENU":

            if text == "➕ افزودن دارو":

                start_add_medication(
                    chat_id,
                    user_id
                )

                return jsonify({
                    "status": "ok"
                })

            if text == "💊 داروهای من":

                show_medications(
                    chat_id,
                    user_id
                )

                return jsonify({
                    "status": "ok"
                })

            if text == "↩️ منوی اصلی":

                start_conversation(
                    chat_id,
                    db_user
                )

                return jsonify({
                    "status": "ok"
                })

            send_message(

                chat_id,

                "لطفاً یکی از گزینه‌های منو رو انتخاب کن.",

                buttons=[
                    ["➕ افزودن دارو"],
                    ["💊 داروهای من"],
                    ["❌ لغو"]
                ]
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # ASK MEDICATION NAME
        # =================================================

        if state == "ASK_MEDICATION_NAME":

            if not text:

                send_message(
                    chat_id,
                    "لطفاً نام دارو رو وارد کن."
                )

                return jsonify({
                    "status": "ok"
                })

            session_data = {
                "medication_name": text
            }

            set_session(
                user_id,
                "ASK_TIMES_PER_DAY",
                session_data
            )

            send_message(

                chat_id,

                f"💊 داروی «{text}»\n\n"

                "این دارو رو روزی چند بار "
                "مصرف می‌کنی؟",

                buttons=[
                    ["1️⃣ یک بار در روز"],
                    ["🔢 چند بار در روز"],
                    ["❌ لغو"]
                ]
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # ASK TIMES PER DAY
        # =================================================

        if state == "ASK_TIMES_PER_DAY":

            if text == "1️⃣ یک بار در روز":

                session_data[
                    "times_per_day"
                ] = 1

                set_session(
                    user_id,
                    "ASK_TIME",
                    session_data
                )

                send_message(

                    chat_id,

                    "⏰ ساعت مصرف رو وارد کن.\n\n"
                    "مثلاً: 08:00"
                )

                return jsonify({
                    "status": "ok"
                })

            if text == "🔢 چند بار در روز":

                set_session(
                    user_id,
                    "ASK_NUMBER_OF_DOSES",
                    session_data
                )

                send_message(

                    chat_id,

                    "🔢 دقیقاً چند بار در روز "
                    "مصرف می‌کنی؟\n\n"
                    "مثلاً: 2"
                )

                return jsonify({
                    "status": "ok"
                })

            send_message(

                chat_id,

                "لطفاً یکی از گزینه‌ها رو انتخاب کن.",

                buttons=[
                    ["1️⃣ یک بار در روز"],
                    ["🔢 چند بار در روز"],
                    ["❌ لغو"]
                ]
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # NUMBER OF DOSES
        # =================================================

        if state == "ASK_NUMBER_OF_DOSES":

            try:

                number = int(text)

            except Exception:

                number = 0

            if number < 2 or number > 10:

                send_message(

                    chat_id,

                    "لطفاً تعداد دفعات رو به صورت "
                    "عدد بین ۲ تا ۱۰ وارد کن.\n\n"
                    "مثلاً: 2"
                )

                return jsonify({
                    "status": "ok"
                })

            session_data[
                "times_per_day"
            ] = number

            session_data[
                "times"
            ] = []

            set_session(
                user_id,
                "ASK_TIME",
                session_data
            )

            send_message(

                chat_id,

                "⏰ ساعت نوبت اول رو وارد کن.\n\n"
                "مثلاً: 08:00"
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # ASK TIME
        # =================================================

        if state == "ASK_TIME":

            time = text

            if not is_valid_time(time):

                send_message(

                    chat_id,

                    "⏰ فرمت ساعت درست نیست.\n\n"

                    "لطفاً ساعت رو به شکل زیر وارد کن:\n"
                    "08:00\n\n"

                    "مثلاً 14:30"
                )

                return jsonify({
                    "status": "ok"
                })

            times_per_day = session_data.get(
                "times_per_day",
                1
            )

            times = session_data.get(
                "times",
                []
            )

            # -------------------------------------------------
            # یک بار در روز
            # -------------------------------------------------

            if times_per_day == 1:

                times = [time]

                session_data[
                    "times"
                ] = times

                show_confirmation(
                    chat_id,
                    user_id,
                    session_data
                )

                return jsonify({
                    "status": "ok"
                })

            # -------------------------------------------------
            # چند بار در روز
            # -------------------------------------------------

            times.append(time)

            session_data[
                "times"
            ] = times

            if len(times) < times_per_day:

                next_dose = (
                    len(times) + 1
                )

                set_session(
                    user_id,
                    "ASK_TIME",
                    session_data
                )

                send_message(

                    chat_id,

                    f"⏰ ساعت نوبت "
                    f"{next_dose} رو وارد کن.\n\n"

                    "مثلاً: 20:00"
                )

                return jsonify({
                    "status": "ok"
                })

            show_confirmation(
                chat_id,
                user_id,
                session_data
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # CONFIRM MEDICATION
        # =================================================

        if state == "CONFIRM_MEDICATION":

            if text == "✅ ثبت دارو":

                save_medication(

                    user_id=user_id,

                    medication_name=session_data[
                        "medication_name"
                    ],

                    times_per_day=session_data[
                        "times_per_day"
                    ],

                    times=session_data[
                        "times"
                    ]
                )

                clear_session(
                    user_id
                )

                send_message(

                    chat_id,

                    "✅ دارو با موفقیت ثبت شد! 💚\n\n"

                    "از این به بعد در ساعت‌های "
                    "تعیین‌شده یادآوری مصرف دارو "
                    "برات ارسال می‌کنم. ⏰",

                    buttons=[
                        ["➕ افزودن دارو"],
                        ["💊 داروهای من"],
                        ["↩️ منوی اصلی"]
                    ]
                )

                return jsonify({
                    "status": "ok"
                })

            if text == "🔄 اصلاح اطلاعات":

                set_session(
                    user_id,
                    "ASK_MEDICATION_NAME",
                    {}
                )

                send_message(

                    chat_id,

                    "🔄 باشه، دوباره شروع می‌کنیم.\n\n"

                    "اسم دارو رو وارد کن:"
                )

                return jsonify({
                    "status": "ok"
                })

            send_message(

                chat_id,

                "لطفاً مشخص کن اطلاعات درست هست یا نه.",

                buttons=[
                    ["✅ ثبت دارو"],
                    ["🔄 اصلاح اطلاعات"],
                    ["❌ لغو"]
                ]
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # Fallback
        # =================================================

        send_message(

            chat_id,

            "متوجه نشدم 🙂\n\n"

            "لطفاً از گزینه‌های موجود استفاده کن."
        )

    except Exception as e:

        print(
            "ERROR in message handler:",
            repr(e)
        )

    return jsonify({
        "status": "ok"
    })


# =========================================================
# Home
# =========================================================

@app.route("/")
def home():

    return (
        "Mahroo backend is running 🚀"
    )


# =========================================================
# Start Server
# =========================================================

if __name__ == "__main__":

    init_database()

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )

