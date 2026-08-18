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


# =========================================================
# Settings
# =========================================================

IRAN_TZ = ZoneInfo("Asia/Tehran")

REMINDER_GRACE_MINUTES = 10
SNOOZE_MINUTES = 5


# =========================================================
# Database
# =========================================================

def get_db_connection():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set"
        )

    return psycopg.connect(DATABASE_URL)


# =========================================================
# Initialize Database
# =========================================================

def init_database():

    if not DATABASE_URL:

        print("ERROR: DATABASE_URL is not set")
        return

    try:

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                # =================================================
                # USERS
                # =================================================

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        bale_user_id BIGINT UNIQUE NOT NULL,
                        first_name TEXT,
                        username TEXT,
                        display_name TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                cur.execute("""
                    ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS display_name TEXT;
                """)


                # =================================================
                # MEDICATIONS
                # =================================================

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS medications (

                        id SERIAL PRIMARY KEY,

                        user_id INTEGER NOT NULL
                            REFERENCES users(id)
                            ON DELETE CASCADE,

                        name TEXT NOT NULL,

                        times_per_day INTEGER NOT NULL,

                        is_active BOOLEAN NOT NULL
                            DEFAULT TRUE,

                        created_at TIMESTAMP
                            DEFAULT CURRENT_TIMESTAMP,

                        ended_at TIMESTAMP
                    );
                """)

                # برای دیتابیس‌های قدیمی
                cur.execute("""
                    ALTER TABLE medications
                    ADD COLUMN IF NOT EXISTS is_active BOOLEAN
                    DEFAULT TRUE;
                """)

                cur.execute("""
                    ALTER TABLE medications
                    ADD COLUMN IF NOT EXISTS ended_at TIMESTAMP;
                """)


                # =================================================
                # MEDICATION SCHEDULES
                # =================================================

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


                # =================================================
                # USER SESSIONS
                # =================================================

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


                # =================================================
                # REMINDER OCCURRENCES
                # =================================================

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reminder_occurrences (

                        id SERIAL PRIMARY KEY,

                        medication_schedule_id INTEGER NOT NULL
                            REFERENCES medication_schedules(id)
                            ON DELETE CASCADE,

                        reminder_date DATE NOT NULL,

                        scheduled_time TEXT NOT NULL,

                        status TEXT NOT NULL
                            DEFAULT 'pending',

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
# Bale
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


    first_name = user.get("first_name")
    username = user.get("username")


    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO users (
                    bale_user_id,
                    first_name,
                    username
                )

                VALUES (
                    %s,
                    %s,
                    %s
                )

                ON CONFLICT (bale_user_id)

                DO UPDATE SET

                    first_name =
                        EXCLUDED.first_name,

                    username =
                        EXCLUDED.username

                RETURNING id, display_name;
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
                SELECT state, data

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
# Time
# =========================================================

def is_valid_time(time_text):

    return bool(
        re.match(
            r"^(?:[01]\d|2[0-3]):[0-5]\d$",
            time_text
        )
    )


def schedule_to_datetime(
    today,
    scheduled_time
):

    try:

        parsed_time = datetime.strptime(
            scheduled_time,
            "%H:%M"
        ).time()


        return datetime.combine(
            today,
            parsed_time
        ).replace(
            tzinfo=IRAN_TZ
        )


    except Exception as e:

        print(
            "Invalid schedule time:",
            scheduled_time,
            repr(e)
        )

        return None


# =========================================================
# Main Menu
# =========================================================

def main_menu(
    chat_id,
    name
):

    send_message(

        chat_id,

        f"سلام {name} جان 🌱\n\n"

        "من «مهرور» هستم؛ "
        "سامانه یادآوری داروهای شما 💚\n\n"

        "از منوی زیر می‌تونی داروهات رو مدیریت کنی.",

        buttons=[

            ["📊 داشبورد من"],

            ["➕ افزودن دارو"],

            ["💊 داروهای من"],

            ["❌ لغو"]
        ]
    )


# =========================================================
# Start
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
# Dashboard
# =========================================================

def show_dashboard(
    chat_id,
    user_id
):

    now = datetime.now(
        IRAN_TZ
    )

    today = now.date()


    with get_db_connection() as conn:

        with conn.cursor() as cur:

            # داروهای فعال
            cur.execute("""
                SELECT COUNT(*)

                FROM medications

                WHERE user_id = %s

                AND is_active = TRUE;
            """, (
                user_id,
            ))

            active_count = cur.fetchone()[0]


            # امروز
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (
                        WHERE ro.status = 'taken'
                    ),

                    COUNT(*) FILTER (
                        WHERE ro.status = 'not_taken'
                    ),

                    COUNT(*) FILTER (
                        WHERE ro.status IN (
                            'pending',
                            'sent',
                            'snoozed'
                        )
                    )

                FROM reminder_occurrences ro

                JOIN medication_schedules ms
                    ON ms.id =
                       ro.medication_schedule_id

                JOIN medications m
                    ON m.id =
                       ms.medication_id

                WHERE m.user_id = %s

                AND ro.reminder_date = %s;
            """, (
                user_id,
                today
            ))

            row = cur.fetchone()

            taken_count = row[0] or 0
            not_taken_count = row[1] or 0
            pending_count = row[2] or 0


            # نوبت بعدی
            cur.execute("""
                SELECT
                    m.name,
                    ms.time

                FROM medication_schedules ms

                JOIN medications m
                    ON m.id =
                       ms.medication_id

                WHERE m.user_id = %s

                AND m.is_active = TRUE

                ORDER BY ms.time;
            """, (
                user_id,
            ))

            schedules = cur.fetchall()


    next_dose = None


    for name, time_text in schedules:

        dt = schedule_to_datetime(
            today,
            time_text
        )

        if dt and dt >= now:

            next_dose = (
                name,
                time_text
            )

            break


    text = (
        "📊 داشبورد مهرور\n\n"

        f"💊 داروهای فعال: "
        f"{active_count}\n\n"

        "📅 وضعیت امروز:\n"

        f"✅ مصرف شده: {taken_count}\n"

        f"⏳ در انتظار: {pending_count}\n"

        f"❌ مصرف نشده: {not_taken_count}\n\n"
    )


    if next_dose:

        text += (
            "⏰ نوبت بعدی:\n"
            f"💊 {next_dose[0]}\n"
            f"🕐 {next_dose[1]}\n"
        )

    else:

        text += (
            "⏰ امروز نوبت دیگری باقی نمانده است. 🌱"
        )


    send_message(

        chat_id,

        text,

        buttons=[

            ["💊 داروهای من"],

            ["➕ افزودن دارو"],

            ["↩️ منوی اصلی"]
        ]
    )


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
                    times_per_day,
                    is_active
                )

                VALUES (
                    %s,
                    %s,
                    %s,
                    TRUE
                )

                RETURNING id;
            """, (
                user_id,
                medication_name,
                times_per_day
            ))


            medication_id = cur.fetchone()[0]


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

    name = data["medication_name"]

    times_per_day = data["times_per_day"]

    times = data["times"]


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
# Medication List
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
                    times_per_day,
                    is_active
                FROM medications
                WHERE user_id = %s
                ORDER BY is_active DESC, id;
            """, (
                user_id,
            ))

            medications = cur.fetchall()


            if not medications:

                send_message(

                    chat_id,

                    "هنوز هیچ دارویی ثبت نکردی 💊",

                    buttons=[

                        ["➕ افزودن دارو"],

                        ["↩️ منوی اصلی"]
                    ]
                )

                return


            text = "💊 داروهای شما:\n\n"

            buttons = []


            for index, medication in enumerate(
                medications,
                start=1
            ):

                medication_id = medication[0]
                name = medication[1]
                times_per_day = medication[2]
                is_active = medication[3]


                status = (
                    "🟢 فعال"
                    if is_active
                    else "🔴 پایان‌یافته"
                )


                text += (
                    f"{index}. 💊 {name}\n"
                    f"   {status}\n"
                    f"   🔢 {times_per_day} بار در روز\n"
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


                # محدودیت اندازه متن دکمه
                button_text = (
                    f"💊 {name}"
                )

                buttons.append(
                    [button_text]
                )


            text += (
                "برای مدیریت هر دارو، "
                "روی نام آن بزن."
            )


    buttons.extend([

        ["➕ افزودن دارو"],

        ["↩️ منوی اصلی"]
    ])


    send_message(

        chat_id,

        text,

        buttons=buttons
    )


# =========================================================
# Get User Medication By Button Text
# =========================================================

def get_medication_by_button(
    user_id,
    text
):

    if not text.startswith("💊 "):

        return None


    name = text[2:].strip()


    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    name,
                    times_per_day,
                    is_active
                FROM medications

                WHERE user_id = %s

                AND name = %s

                ORDER BY id DESC

                LIMIT 1;
            """, (
                user_id,
                name
            ))

            return cur.fetchone()


# =========================================================
# Medication Management
# =========================================================

def show_medication_management(
    chat_id,
    user_id,
    medication_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    name,
                    times_per_day,
                    is_active
                FROM medications

                WHERE id = %s

                AND user_id = %s;
            """, (
                medication_id,
                user_id
            ))

            medication = cur.fetchone()


            if not medication:

                send_message(
                    chat_id,
                    "دارو پیدا نشد."
                )

                return


            cur.execute("""
                SELECT
                    id,
                    dose_number,
                    time

                FROM medication_schedules

                WHERE medication_id = %s

                ORDER BY dose_number;
            """, (
                medication_id,
            ))

            schedules = cur.fetchall()


    name = medication[1]
    is_active = medication[3]


    status = (
        "🟢 فعال"
        if is_active
        else "🔴 پایان‌یافته"
    )


    text = (
        f"💊 {name}\n\n"
        f"وضعیت: {status}\n\n"
        "⏰ ساعت‌های مصرف:\n"
    )


    for schedule in schedules:

        text += (
            f"{schedule[1]}. {schedule[2]}\n"
        )


    buttons = [

        ["✏️ تغییر ساعت مصرف"],

        ["🗑 حذف دارو"]
    ]


    if is_active:

        buttons.append(
            ["🛑 پایان مصرف دارو"]
        )

    else:

        buttons.append(
            ["🟢 فعال کردن دوباره"]
        )


    buttons.append(
        ["↩️ داروهای من"]
    )


    set_session(

        user_id,

        "MEDICATION_MANAGEMENT",

        {
            "medication_id": medication_id
        }
    )


    send_message(

        chat_id,

        text,

        buttons=buttons
    )


# =========================================================
# End Medication
# =========================================================

def end_medication(
    user_id,
    medication_id
):

    now = datetime.now(
        IRAN_TZ
    )


    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE medications

                SET
                    is_active = FALSE,
                    ended_at = %s

                WHERE id = %s

                AND user_id = %s;
            """, (
                now,
                medication_id,
                user_id
            ))


            # Reminderهای pending آینده دیگر ارسال نشوند
            cur.execute("""
                UPDATE reminder_occurrences ro

                SET status = 'cancelled'

                FROM medication_schedules ms

                WHERE ro.medication_schedule_id = ms.id

                AND ms.medication_id = %s

                AND ro.status IN (
                    'pending',
                    'sent',
                    'snoozed'
                )

                AND ro.reminder_date >= %s;
            """, (
                medication_id,
                now.date()
            ))


        conn.commit()


# =========================================================
# Reactivate Medication
# =========================================================

def reactivate_medication(
    user_id,
    medication_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE medications

                SET
                    is_active = TRUE,
                    ended_at = NULL

                WHERE id = %s

                AND user_id = %s;
            """, (
                medication_id,
                user_id
            ))

        conn.commit()


# =========================================================
# Delete Medication
# =========================================================

def delete_medication(
    user_id,
    medication_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                DELETE FROM medications

                WHERE id = %s

                AND user_id = %s;
            """, (
                medication_id,
                user_id
            ))

        conn.commit()


# =========================================================
# Change Medication Times
# =========================================================

def start_change_times(
    chat_id,
    user_id,
    medication_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    name,
                    times_per_day

                FROM medications

                WHERE id = %s

                AND user_id = %s;
            """, (
                medication_id,
                user_id
            ))

            row = cur.fetchone()


    if not row:

        send_message(
            chat_id,
            "دارو پیدا نشد."
        )

        return


    name = row[0]
    times_per_day = row[1]


    set_session(

        user_id,

        "EDIT_TIMES",

        {
            "medication_id": medication_id,
            "medication_name": name,
            "times_per_day": times_per_day,
            "times": []
        }
    )


    send_message(

        chat_id,

        f"✏️ تغییر ساعت مصرف «{name}»\n\n"

        f"تعداد نوبت‌ها: {times_per_day}\n\n"

        "⏰ ساعت نوبت اول را وارد کن.\n\n"

        "مثلاً:\n"
        "08:00"
    )


# =========================================================
# Save Changed Times
# =========================================================

def save_changed_times(
    user_id,
    medication_id,
    times
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                DELETE FROM medication_schedules

                WHERE medication_id = %s;
            """, (
                medication_id,
            ))


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


            # occurrenceهای pending/snoozed قبلی
            # دیگر با برنامه جدید معتبر نیستند
            cur.execute("""
                UPDATE reminder_occurrences ro

                SET status = 'cancelled'

                WHERE ro.medication_schedule_id IN (

                    SELECT id

                    FROM medication_schedules

                    WHERE medication_id = %s
                )

                AND ro.status IN (
                    'pending',
                    'sent',
                    'snoozed'
                );
            """, (
                medication_id,
            ))


        conn.commit()


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
# Active Reminder
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

                AND m.is_active = TRUE

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

            return cur.fetchone()


# =========================================================
# Reminder Actions
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


    if text == REMINDER_TAKEN:

        mark_reminder_taken(
            reminder_id
        )


        send_message(

            chat_id,

            f"✅ ثبت شد.\n\n"

            f"مصرف «{medication_name}» "
            "انجام‌شده ثبت شد. 💚\n\n"

            "برای این نوبت دیگر یادآوری نمی‌کنم.",

            buttons=[

                ["📊 داشبورد من"],

                ["💊 داروهای من"],

                ["➕ افزودن دارو"]
            ]
        )


        return True


    if text == REMINDER_NOT_TAKEN:

        mark_reminder_not_taken(
            reminder_id
        )


        send_message(

            chat_id,

            f"ثبت شد. ❌\n\n"

            f"نوبت «{medication_name}» "
            "مصرف‌نشده ثبت شد.\n\n"

            "برای این نوبت دیگر یادآوری نمی‌کنم."
        )


        return True


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

            "۵ دقیقه دیگه دوباره یادآوری می‌کنم. ⏰",

            buttons=[

                [REMINDER_TAKEN],

                [REMINDER_NOT_TAKEN],

                [REMINDER_SNOOZE]
            ]
        )


        return True


    return False


# =========================================================
# Create Due Occurrences
# =========================================================

def create_due_occurrences(
    cur,
    today,
    now
):

    window_start = (
        now
        - timedelta(
            minutes=REMINDER_GRACE_MINUTES
        )
    )


    cur.execute("""
        SELECT
            ms.id,
            ms.time

        FROM medication_schedules ms

        JOIN medications m
            ON m.id =
               ms.medication_id

        WHERE m.is_active = TRUE

        ORDER BY ms.time;
    """)


    schedules = cur.fetchall()

    created = 0


    for schedule_id, scheduled_time in schedules:

        scheduled_datetime = schedule_to_datetime(
            today,
            scheduled_time
        )


        if not scheduled_datetime:
            continue


        if not (
            window_start
            <= scheduled_datetime
            <= now
        ):
            continue


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
# Pending Reminders
# =========================================================

def get_pending_reminders(
    cur,
    today,
    now
):

    window_start = (
        now
        - timedelta(
            minutes=REMINDER_GRACE_MINUTES
        )
    )


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

        AND m.is_active = TRUE

        ORDER BY
            ro.scheduled_time,
            ro.id;
    """, (
        today,
    ))


    reminders = cur.fetchall()

    valid_reminders = []


    for reminder in reminders:

        scheduled_datetime = schedule_to_datetime(
            today,
            reminder[2]
        )


        if not scheduled_datetime:
            continue


        if (
            window_start
            <= scheduled_datetime
            <= now
        ):

            valid_reminders.append(
                reminder
            )


    return valid_reminders


# =========================================================
# Normal Reminder
# =========================================================

def send_normal_reminder(
    reminder,
    now,
    cur
):

    reminder_id = reminder[0]
    medication_name = reminder[3]
    bale_user_id = reminder[4]
    display_name = reminder[5]
    dose_number = reminder[6]
    medication_time = reminder[2]


    greeting = (
        f"{display_name} جان 🌱"
        if display_name
        else "دوست عزیز 🌱"
    )


    message = (

        "⏰ یادآوری مصرف دارو\n\n"

        f"{greeting}\n\n"

        f"💊 زمان مصرف "
        f"«{medication_name}» "
        "رسیده است.\n\n"

        f"🕐 ساعت: {medication_time}\n"

        f"💊 نوبت مصرف: {dose_number}\n\n"

        "اگر مصرف کردی، ثبتش کن."
    )


    response = send_message(

        bale_user_id,

        message,

        buttons=[

            [REMINDER_TAKEN],

            [REMINDER_SNOOZE],

            [REMINDER_NOT_TAKEN]
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

        return True


    return False


# =========================================================
# Snoozed Reminder
# =========================================================

def send_snoozed_reminder(
    reminder,
    now,
    cur
):

    reminder_id = reminder[0]
    medication_time = reminder[1]
    medication_name = reminder[3]
    bale_user_id = reminder[4]
    display_name = reminder[5]
    dose_number = reminder[6]


    greeting = (
        f"{display_name} جان 🌱"
        if display_name
        else "دوست عزیز 🌱"
    )


    message = (

        "⏰ یادآوری مجدد مصرف دارو\n\n"

        f"{greeting}\n\n"

        f"💊 داروی «{medication_name}» "
        "هنوز در انتظار ثبت وضعیت است.\n\n"

        f"🕐 نوبت: {medication_time}\n"

        f"💊 شماره نوبت: {dose_number}\n\n"

        "آیا مصرفش کردی؟"
    )


    response = send_message(

        bale_user_id,

        message,

        buttons=[

            [REMINDER_TAKEN],

            [REMINDER_NOT_TAKEN],

            [REMINDER_SNOOZE]
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

        return True


    return False


# =========================================================
# Reminder System
# =========================================================

@app.route(
    "/check-reminders",
    methods=["GET"]
)
def check_reminders():

    now = datetime.now(
        IRAN_TZ
    )

    today = now.date()

    sent_count = 0
    snooze_count = 0
    created_count = 0
    error_count = 0


    try:

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                # ---------------------------------------------
                # Create due occurrences
                # ---------------------------------------------

                created_count = create_due_occurrences(

                    cur,

                    today,

                    now
                )


                # ---------------------------------------------
                # Normal reminders
                # ---------------------------------------------

                reminders = get_pending_reminders(

                    cur,

                    today,

                    now
                )


                for reminder in reminders:

                    if send_normal_reminder(
                        reminder,
                        now,
                        cur
                    ):

                        sent_count += 1

                    else:

                        error_count += 1


                # ---------------------------------------------
                # Snoozed reminders
                # ---------------------------------------------

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

                    AND m.is_active = TRUE

                    ORDER BY
                        ro.snooze_until,
                        ro.id;
                """, (
                    today,
                    now
                ))


                snoozed = cur.fetchall()


                for reminder in snoozed:

                    if send_snoozed_reminder(
                        reminder,
                        now,
                        cur
                    ):

                        snooze_count += 1

                    else:

                        error_count += 1


            conn.commit()


        return jsonify({

            "status": "ok",

            "current_time":
                now.strftime("%H:%M:%S"),

            "created":
                created_count,

            "sent":
                sent_count,

            "snoozed_sent":
                snooze_count,

            "errors":
                error_count
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
# Cancel
# =========================================================

def cancel_conversation(
    chat_id,
    user_id
):

    clear_session(user_id)


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


        db_user = get_or_create_user(
            user
        )


        if not db_user:

            return jsonify({
                "status": "ok"
            })


        user_id = db_user["id"]


        # =====================================================
        # Reminder Buttons
        # =====================================================

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


        # =====================================================
        # Cancel
        # =====================================================

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


        # =====================================================
        # Start
        # =====================================================

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


        # =====================================================
        # Session
        # =====================================================

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


        # =====================================================
        # ASK NAME
        # =====================================================

        if state == "ASK_NAME":

            if not text:

                send_message(
                    chat_id,
                    "لطفاً اسمتون رو وارد کنید 🙂"
                )

                return jsonify({
                    "status": "ok"
                })


            save_display_name(
                user_id,
                text
            )


            set_session(
                user_id,
                "MAIN_MENU",
                {}
            )


            main_menu(
                chat_id,
                text
            )


            return jsonify({
                "status": "ok"
            })


        # =====================================================
        # MAIN MENU
        # =====================================================

        if state == "MAIN_MENU":

            if text == "📊 داشبورد من":

                show_dashboard(
                    chat_id,
                    user_id
                )

                return jsonify({
                    "status": "ok"
                })


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

                    ["📊 داشبورد من"],

                    ["➕ افزودن دارو"],

                    ["💊 داروهای من"],

                    ["❌ لغو"]
                ]
            )


            return jsonify({
                "status": "ok"
            })


        # =====================================================
        # Medication Button
        # =====================================================

        medication = get_medication_by_button(
            user_id,
            text
        )


        if medication:

            show_medication_management(

                chat_id,
                user_id,
                medication[0]
            )

            return jsonify({
                "status": "ok"
            })


        # =====================================================
        # MEDICATION MANAGEMENT
        # =====================================================

        if state == "MEDICATION_MANAGEMENT":

            medication_id = session_data.get(
                "medication_id"
            )


            if text == "✏️ تغییر ساعت مصرف":

                start_change_times(

                    chat_id,
                    user_id,
                    medication_id
                )

                return jsonify({
                    "status": "ok"
                })


            if text == "🛑 پایان مصرف دارو":

                end_medication(

                    user_id,
                    medication_id
                )


                clear_session(
                    user_id
                )


                send_message(

                    chat_id,

                    "🛑 مصرف این دارو به پایان رسید.\n\n"

                    "از این به بعد برای این دارو "
                    "Reminder جدیدی ارسال نمی‌کنم. 💚",

                    buttons=[

                        ["📊 داشبورد من"],

                        ["💊 داروهای من"],

                        ["↩️ منوی اصلی"]
                    ]
                )


                return jsonify({
                    "status": "ok"
                })


            if text == "🟢 فعال کردن دوباره":

                reactivate_medication(

                    user_id,
                    medication_id
                )


                clear_session(
                    user_id
                )


                send_message(

                    chat_id,

                    "🟢 دارو دوباره فعال شد.\n\n"

                    "از این به بعد Reminderهای "
                    "آن دوباره فعال هستند. ⏰",

                    buttons=[

                        ["💊 داروهای من"],

                        ["📊 داشبورد من"]
                    ]
                )


                return jsonify({
                    "status": "ok"
                })


            if text == "🗑 حذف دارو":

                set_session(

                    user_id,

                    "CONFIRM_DELETE",

                    {
                        "medication_id":
                            medication_id
                    }
                )


                send_message(

                    chat_id,

                    "⚠️ مطمئنی می‌خوای این دارو "
                    "رو حذف کنی؟\n\n"

                    "با حذف دارو، اطلاعات مربوط "
                    "به نوبت‌های آن هم حذف می‌شود.",

                    buttons=[

                        ["✅ بله، حذف شود"],

                        ["❌ لغو"]
                    ]
                )


                return jsonify({
                    "status": "ok"
                })


            if text == "↩️ داروهای من":

                clear_session(
                    user_id
                )


                show_medications(
                    chat_id,
                    user_id
                )

                return jsonify({
                    "status": "ok"
                })


        # =====================================================
        # DELETE CONFIRM
        # =====================================================

        if state == "CONFIRM_DELETE":

            medication_id = session_data.get(
                "medication_id"
            )


            if text == "✅ بله، حذف شود":

                delete_medication(

                    user_id,
                    medication_id
                )


                clear_session(
                    user_id
                )


                send_message(

                    chat_id,

                    "🗑 دارو با موفقیت حذف شد.",

                    buttons=[

                        ["💊 داروهای من"],

                        ["📊 داشبورد من"],

                        ["↩️ منوی اصلی"]
                    ]
                )


                return jsonify({
                    "status": "ok"
                })


        # =====================================================
        # ASK MEDICATION NAME
        # =====================================================

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


        # =====================================================
        # ASK TIMES PER DAY
        # =====================================================

        if state == "ASK_TIMES_PER_DAY":

            if text == "1️⃣ یک بار در روز":

                session_data["times_per_day"] = 1
                session_data["times"] = []


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


        # =====================================================
        # NUMBER OF DOSES
        # =====================================================

        if state == "ASK_NUMBER_OF_DOSES":

            try:

                number = int(text)

            except Exception:

                number = 0


            if number < 2 or number > 10:

                send_message(

                    chat_id,

                    "لطفاً تعداد دفعات رو به صورت "
                    "عدد بین ۲ تا ۱۰ وارد کن."
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


        # =====================================================
        # ASK TIME
        # =====================================================

        if state == "ASK_TIME":

            time = text


            if not is_valid_time(time):

                send_message(

                    chat_id,

                    "⏰ فرمت ساعت درست نیست.\n\n"

                    "مثلاً:\n"
                    "08:00\n"
                    "14:30"
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


            if time in times:

                send_message(

                    chat_id,

                    "⚠️ این ساعت رو قبلاً وارد کردی."
                )


                return jsonify({
                    "status": "ok"
                })


            times.append(time)

            session_data["times"] = times


            if len(times) < times_per_day:

                next_dose = len(times) + 1


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


        # =====================================================
        # CONFIRM MEDICATION
        # =====================================================

        if state == "CONFIRM_MEDICATION":

            if text == "✅ ثبت دارو":

                save_medication(

                    user_id,

                    session_data[
                        "medication_name"
                    ],

                    session_data[
                        "times_per_day"
                    ],

                    session_data[
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

                        ["📊 داشبورد من"],

                        ["➕ افزودن دارو"],

                        ["💊 داروهای من"]
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


        # =====================================================
        # EDIT TIMES
        # =====================================================

        if state == "EDIT_TIMES":

            if not is_valid_time(text):

                send_message(

                    chat_id,

                    "⏰ ساعت درست نیست.\n\n"

                    "مثلاً 08:00"
                )


                return jsonify({
                    "status": "ok"
                })


            times = session_data.get(
                "times",
                []
            )


            if text in times:

                send_message(

                    chat_id,

                    "⚠️ این ساعت قبلاً وارد شده."
                )


                return jsonify({
                    "status": "ok"
                })


            times.append(text)

            session_data["times"] = times


            required = session_data[
                "times_per_day"
            ]


            if len(times) < required:

                next_dose = len(times) + 1


                set_session(

                    user_id,

                    "EDIT_TIMES",

                    session_data
                )


                send_message(

                    chat_id,

                    f"⏰ ساعت نوبت "
                    f"{next_dose} رو وارد کن."
                )


                return jsonify({
                    "status": "ok"
                })


            save_changed_times(

                user_id,

                session_data[
                    "medication_id"
                ],

                times
            )


            clear_session(
                user_id
            )


            send_message(

                chat_id,

                "✅ ساعت‌های مصرف با موفقیت تغییر کردند.",

                buttons=[

                    ["💊 داروهای من"],

                    ["📊 داشبورد من"],

                    ["↩️ منوی اصلی"]
                ]
            )


            return jsonify({
                "status": "ok"
            })


        # =====================================================
        # FALLBACK
        # =====================================================

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

    return "Mahroo backend is running 🚀"


# =========================================================
# Start
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
