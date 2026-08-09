from flask import Flask, request, jsonify
import requests
import os
import psycopg
import json
import re
from datetime import datetime
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


# =========================================================
# Database
# =========================================================

def get_db_connection():
    return psycopg.connect(DATABASE_URL)


def init_database():

    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set")
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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # -------------------------------------------------
                # Reminder logs
                #
                # جلوگیری از ارسال دوباره یک یادآوری در همان روز
                # -------------------------------------------------

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reminder_logs (
                        id SERIAL PRIMARY KEY,

                        medication_schedule_id INTEGER NOT NULL
                            REFERENCES medication_schedules(id)
                            ON DELETE CASCADE,

                        reminder_date DATE NOT NULL,

                        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                        UNIQUE (
                            medication_schedule_id,
                            reminder_date
                        )
                    );
                """)

            conn.commit()

        print("Database initialized successfully.")

    except Exception as e:
        print("Database initialization error:", repr(e))


# =========================================================
# Bale API
# =========================================================

def send_message(chat_id, text, buttons=None):

    if not BALE_API:
        print("ERROR: BALE_BOT_TOKEN is not set")
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

        print("Bale send error:", repr(e))
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

                VALUES (%s, %s, %s)

                ON CONFLICT (bale_user_id)

                DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    username = EXCLUDED.username

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

def set_session(user_id, state, data=None):

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
                    state = EXCLUDED.state,
                    data = EXCLUDED.data,
                    updated_at = CURRENT_TIMESTAMP;
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
            """, (user_id,))

            row = cur.fetchone()

    if not row:
        return None

    try:
        data = json.loads(row[1]) if row[1] else {}
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
            """, (user_id,))

        conn.commit()


# =========================================================
# Display Name
# =========================================================

def save_display_name(user_id, display_name):

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

                VALUES (%s, %s, %s)

                RETURNING id;
            """, (
                user_id,
                medication_name,
                times_per_day
            ))

            medication_id = cur.fetchone()[0]

            for index, time in enumerate(times, start=1):

                cur.execute("""
                    INSERT INTO medication_schedules (
                        medication_id,
                        dose_number,
                        time
                    )

                    VALUES (%s, %s, %s);
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

    pattern = r"^(?:[01]\d|2[0-3]):[0-5]\d$"

    return bool(
        re.match(pattern, time_text)
    )


# =========================================================
# Main Menu
# =========================================================

def main_menu(chat_id, name):

    send_message(
        chat_id,

        f"خیلی خوشحالم که با من آشنا شدی {name} جان 🌱\n\n"
        "من «مهرور» هستم؛ سامانه یادآوری داروهای شما 💚\n\n"
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

def start_conversation(chat_id, db_user):

    display_name = db_user.get("display_name")

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
            "من مهرور هستم؛ سامانه یادآوری داروهای شما 💚\n\n"
            "خوشحال می‌شم بدونم با چه اسمی صداتون بزنم؟"
        )

        set_session(
            db_user["id"],
            "ASK_NAME",
            {}
        )


# =========================================================
# List Medications
# =========================================================

def show_medications(chat_id, user_id):

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
            """, (user_id,))

            medications = cur.fetchall()

            if not medications:

                send_message(
                    chat_id,

                    "هنوز هیچ دارویی ثبت نکردی 💊\n\n"
                    "برای شروع، روی «➕ افزودن دارو» بزن.",

                    buttons=[
                        ["➕ افزودن دارو"],
                        ["↩️ منوی اصلی"]
                    ]
                )

                return

            text = "💊 داروهای ثبت‌شده شما:\n\n"

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
                """, (medication_id,))

                schedules = cur.fetchall()

                for schedule in schedules:

                    text += f"   ⏰ {schedule[0]}\n"

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

def cancel_conversation(chat_id, user_id):

    clear_session(user_id)

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT display_name
                FROM users
                WHERE id = %s;
            """, (user_id,))

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

def start_add_medication(chat_id, user_id):

    set_session(
        user_id,
        "ASK_MEDICATION_NAME",
        {}
    )

    send_message(
        chat_id,

        "💊 خیلی خوب.\n\n"
        "اسم دارویی که می‌خوای ثبت کنی رو بنویس:"
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
# REMINDER SYSTEM
# =========================================================

@app.route("/check-reminders", methods=["GET"])
def check_reminders():

    print("Reminder check started.")

    now = datetime.now(IRAN_TZ)

    current_time = now.strftime("%H:%M")
    today = now.date()

    sent_count = 0
    error_count = 0

    try:

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                cur.execute("""
                    SELECT
                        ms.id,
                        ms.time,
                        m.name,
                        u.id,
                        u.bale_user_id,
                        u.display_name,
                        ms.dose_number
                    FROM medication_schedules ms

                    JOIN medications m
                        ON m.id = ms.medication_id

                    JOIN users u
                        ON u.id = m.user_id

                    WHERE ms.time = %s
                    ORDER BY ms.id;
                """, (current_time,))

                reminders = cur.fetchall()

                print(
                    f"Found {len(reminders)} reminder(s) "
                    f"for {current_time}"
                )

                for reminder in reminders:

                    schedule_id = reminder[0]
                    medication_time = reminder[1]
                    medication_name = reminder[2]
                    user_id = reminder[3]
                    bale_user_id = reminder[4]
                    display_name = reminder[5]
                    dose_number = reminder[6]

                    # -------------------------------------------------
                    # جلوگیری از ارسال دوباره
                    # -------------------------------------------------

                    cur.execute("""
                        SELECT id
                        FROM reminder_logs
                        WHERE medication_schedule_id = %s
                        AND reminder_date = %s;
                    """, (
                        schedule_id,
                        today
                    ))

                    already_sent = cur.fetchone()

                    if already_sent:
                        print(
                            "Already sent:",
                            medication_name,
                            medication_time,
                            bale_user_id
                        )
                        continue

                    # -------------------------------------------------
                    # ارسال پیام
                    # -------------------------------------------------

                    if display_name:

                        greeting = f"{display_name} جان 🌱"

                    else:

                        greeting = "دوست عزیز 🌱"

                    message = (
                        f"⏰ یادآوری مصرف دارو\n\n"
                        f"{greeting}\n\n"
                        f"💊 زمان مصرف «{medication_name}» "
                        f"رسیده است.\n\n"
                        f"🕐 ساعت: {medication_time}\n"
                        f"💊 نوبت مصرف: {dose_number}"
                    )

                    response = send_message(
                        bale_user_id,
                        message
                    )

                    # -------------------------------------------------
                    # ثبت موفقیت
                    # -------------------------------------------------

                    if response and response.ok:

                        cur.execute("""
                            INSERT INTO reminder_logs (
                                medication_schedule_id,
                                reminder_date
                            )

                            VALUES (%s, %s)

                            ON CONFLICT (
                                medication_schedule_id,
                                reminder_date
                            )

                            DO NOTHING;
                        """, (
                            schedule_id,
                            today
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

            conn.commit()

        print(
            f"Reminder check finished. "
            f"Sent={sent_count}, Errors={error_count}"
        )

        return jsonify({
            "status": "ok",
            "current_time": current_time,
            "sent": sent_count,
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

@app.route("/message", methods=["POST"])
def receive_message():

    data = request.json

    print("Received:", data)

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

        chat_id = chat.get("id")

        text = message.get(
            "text",
            ""
        ).strip()

        # -------------------------------------------------
        # User
        # -------------------------------------------------

        db_user = get_or_create_user(user)

        if not db_user:

            return jsonify({
                "status": "ok"
            })

        user_id = db_user["id"]

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
                "این دارو رو روزی چند بار مصرف می‌کنی؟",

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

                session_data["times_per_day"] = 1

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

            session_data["times_per_day"] = number
            session_data["times"] = []

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

            if times_per_day == 1:

                times = [time]

                session_data["times"] = times

                show_confirmation(
                    chat_id,
                    user_id,
                    session_data
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
                    "از این به بعد در ساعت‌های تعیین‌شده "
                    "یادآوری مصرف دارو برات ارسال می‌کنم. ⏰",

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

    return "Mahroo backend is running 🚀"


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
