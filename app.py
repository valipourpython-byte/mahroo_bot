from flask import Flask, request, jsonify
import requests
import os
import psycopg
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import jdatetime

# =========================================================
# APP
# =========================================================

app = Flask(__name__)


# =========================================================
# ENVIRONMENT
# =========================================================

TOKEN = os.getenv("BALE_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "inclusionai/ling-3.0-flash-sante:free"
)

if TOKEN:
    BALE_API = f"https://tapi.bale.ai/bot{TOKEN}/sendMessage"
else:
    BALE_API = None


# =========================================================
# SETTINGS
# =========================================================

IRAN_TZ = ZoneInfo("Asia/Tehran")

REMINDER_GRACE_MINUTES = 10
SNOOZE_MINUTES = 5

MAX_BALE_MESSAGE_LENGTH = 3800


# =========================================================
# MAHROO TABLE NAMES
#
# IMPORTANT:
# These are the ONLY tables used by the Mahroo
# user / medication / reminder system.
#
# Old tables are intentionally NOT used.
# =========================================================

USERS_TABLE = "mahroo_users"
MEDICATIONS_TABLE = "mahroo_medications"
SCHEDULES_TABLE = "mahroo_medication_schedules"
SESSIONS_TABLE = "mahroo_user_sessions"
REMINDERS_TABLE = "mahroo_reminder_occurrences"


# =========================================================
# DATABASE CONNECTION
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
# DATABASE INITIALIZATION
# =========================================================

def init_database():

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            # =================================================
            # MAHROO USERS
            # =================================================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS mahroo_users (
                    id BIGSERIAL PRIMARY KEY,
                    bale_user_id TEXT UNIQUE NOT NULL,
                    chat_id TEXT,
                    display_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # =================================================
            # MAHROO MEDICATIONS
            # =================================================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS mahroo_medications (
                    id BIGSERIAL PRIMARY KEY,

                    user_id BIGINT NOT NULL
                        REFERENCES mahroo_users(id)
                        ON DELETE CASCADE,

                    name TEXT NOT NULL,

                    doses_per_day INTEGER,

                    number_of_doses INTEGER,

                    active BOOLEAN DEFAULT TRUE,

                    created_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP,

                    updated_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # =================================================
            # MAHROO MEDICATION SCHEDULES
            # =================================================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS mahroo_medication_schedules (
                    id BIGSERIAL PRIMARY KEY,

                    medication_id BIGINT NOT NULL
                        REFERENCES mahroo_medications(id)
                        ON DELETE CASCADE,

                    scheduled_time TEXT NOT NULL,

                    active BOOLEAN DEFAULT TRUE,

                    created_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # =================================================
            # MAHROO USER SESSIONS
            # =================================================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS mahroo_user_sessions (
                    user_id BIGINT PRIMARY KEY
                        REFERENCES mahroo_users(id)
                        ON DELETE CASCADE,

                    state TEXT NOT NULL,

                    data JSONB DEFAULT '{}'::jsonb,

                    updated_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # =================================================
            # MAHROO REMINDER OCCURRENCES
            # =================================================

            cur.execute("""
                CREATE TABLE IF NOT EXISTS mahroo_reminder_occurrences (
                    id BIGSERIAL PRIMARY KEY,

                    medication_id BIGINT NOT NULL
                        REFERENCES mahroo_medications(id)
                        ON DELETE CASCADE,

                    schedule_id BIGINT NOT NULL
                        REFERENCES mahroo_medication_schedules(id)
                        ON DELETE CASCADE,

                    user_id BIGINT NOT NULL
                        REFERENCES mahroo_users(id)
                        ON DELETE CASCADE,

                    scheduled_for TIMESTAMP NOT NULL,

                    status TEXT NOT NULL
                        DEFAULT 'pending',

                    sent_at TIMESTAMP,

                    snoozed_until TIMESTAMP,

                    taken_at TIMESTAMP,

                    not_taken_at TIMESTAMP,

                    created_at TIMESTAMP
                        DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # =================================================
            # INDEXES
            # =================================================

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_mahroo_medications_user
                ON mahroo_medications(user_id);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_mahroo_schedules_medication
                ON mahroo_medication_schedules(medication_id);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_mahroo_sessions_user
                ON mahroo_user_sessions(user_id);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_mahroo_reminders_user
                ON mahroo_reminder_occurrences(user_id);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_mahroo_reminders_status
                ON mahroo_reminder_occurrences(status);
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_mahroo_reminders_scheduled
                ON mahroo_reminder_occurrences(scheduled_for);
            """)

            conn.commit()

            print(
                "Mahroo database initialization completed successfully."
            )


# =========================================================
# MAIN / PERSISTENT MENU
# =========================================================

MAIN_MENU_BUTTONS = [
    ["📊 داشبورد من"],
    ["➕ افزودن دارو"],
    ["💊 داروهای من"],
    ["🔎 جستجوی دارو"],
    ["💬 سؤال دارویی"],
    ["❌ لغو"]
]


# =========================================================
# BALE SEND MESSAGE
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

    if not text:

        text = (
            "پاسخی برای نمایش وجود ندارد."
        )

    # -----------------------------------------------------
    # Message length protection
    # -----------------------------------------------------

    if len(text) > MAX_BALE_MESSAGE_LENGTH:

        text = (
            text[:MAX_BALE_MESSAGE_LENGTH]
            + "\n\n…"
        )

    # -----------------------------------------------------
    # Build keyboard
    #
    # Extra buttons appear above the permanent menu.
    # -----------------------------------------------------

    keyboard = []

    if buttons:

        for row in buttons:

            if row not in keyboard:

                keyboard.append(row)

    # -----------------------------------------------------
    # Permanent main menu
    # -----------------------------------------------------

    for row in MAIN_MENU_BUTTONS:

        if row not in keyboard:

            keyboard.append(row)

    payload = {

        "chat_id":
            str(chat_id),

        "text":
            text,

        "reply_markup": {

            "keyboard":
                keyboard,

            "resize_keyboard":
                True
        }
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

        if not response.ok:

            print(
                "Bale HTTP error:",
                response.status_code
            )

            return None

        try:

            data = response.json()

            if isinstance(data, dict):

                if data.get("ok") is False:

                    print(
                        "Bale API returned ok=false:",
                        data
                    )

                    return None

        except Exception as e:

            print(
                "Bale response JSON error:",
                repr(e)
            )

        return response

    except Exception as e:

        print(
            "Bale send error:",
            repr(e)
        )

        return None


# =========================================================
# LLM
# =========================================================

def ask_llm(
    user_question,
    drug_context=""
):

    if not OPENROUTER_API_KEY:

        print(
            "ERROR: OPENROUTER_API_KEY is not configured.",
            flush=True
        )

        return None

    system_prompt = """
تو دستیار هوشمند دارویی «مهرو» هستی.

وظیفه تو پاسخ‌گویی کوتاه، واضح و فارسی به پرسش‌های
کاربران درباره داروها است.

قوانین:

1. فقط درباره اطلاعات دارویی پاسخ بده.

2. اطلاعات موجود در CONTEXT منبع اصلی پاسخ است.

3. فقط از اطلاعات موجود در CONTEXT استفاده کن.
اطلاعات دارویی جدید از خودت اضافه نکن.

4. اگر اطلاعات کافی در CONTEXT وجود ندارد، بگو:
«اطلاعات کافی درباره این دارو در پایگاه داده مهرو موجود نیست.»

5. پاسخ را برای یک کاربر عادی و به زبان فارسی بنویس.

6. نام انگلیسی دارو را در صورت مفید بودن داخل پرانتز بیاور.

7. پاسخ کوتاه و مستقیم باشد.

8. اگر سؤال درباره موارد مصرف دارو است، فقط موارد مصرف
موجود در CONTEXT را به صورت خلاصه بیان کن.

9. اگر سؤال درباره عوارض، تداخلات، هشدارها یا منع مصرف است،
فقط اطلاعات مربوط به همان بخش را از CONTEXT استخراج کن.

10. برای دوز شخصی، شروع، قطع یا تغییر مقدار مصرف دارو،
توصیه قطعی و شخصی‌سازی‌شده نده و کاربر را به پزشک
یا داروساز ارجاع بده.

11. تشخیص پزشکی قطعی ارائه نکن.

12. بسیار مهم:
پاسخ نهایی را مستقیماً در بخش content قرار بده.
نیازی به توضیح مراحل فکر کردن، reasoning یا تحلیل سؤال نیست.
فقط پاسخ نهایی کاربر را تولید کن.
"""

    user_prompt = f"""
سؤال کاربر:

{user_question}

==============================

CONTEXT دارویی مهرو:

{drug_context if drug_context else "اطلاعات دارویی مشخصی برای این سؤال پیدا نشد."}

==============================

اکنون فقط پاسخ نهایی فارسی را بنویس
پاسخ باید فارسی روان، طبیعی و قابل فهم برای کاربر عادی باشد.

از ترجمه تحت‌اللفظی، ترکیب‌های عجیب، کلمات نامفهوم،
غلط‌های تایپی و واژه‌های ساختگی خودداری کن.

اگر یک عبارت پزشکی در CONTEXT به انگلیسی وجود دارد،
آن را به یک معادل رایج و طبیعی فارسی تبدیل کن.

مثلاً:
"menstrual cramps" → "گرفتگی‌های قاعدگی"
"skin rash" → "راش پوستی یا بثورات پوستی"
"common cold" → "سرماخوردگی"
"consult a doctor" → "با پزشک یا داروساز مشورت کنید"

هرگز کلمه‌ای را که از نظر معنایی نامطمئن هستی
به صورت تحت‌اللفظی ترجمه نکن.

اگر ترجمه یک اصطلاح پزشکی نامطمئن است،
بهتر است اصطلاح انگلیسی را داخل پرانتز بیاوری
تا اینکه یک واژه فارسی نامفهوم بسازی.

پاسخ نهایی را قبل از ارسال از نظر فارسی بودن،
معنی جمله‌ها و غلط‌های واضح بررسی کن.
"""

    payload = {
        "model": OPENROUTER_MODEL,

        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ],

        "temperature": 0.1,

        "max_tokens": 800,

        "reasoning": {
            "enabled": False
        }
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://htvsai.app",
        "X-Title": "Mahroo"
    }

    try:

        print(
            "========== ASK_LLM CALLED ==========",
            flush=True
        )

        print(
            "Question:",
            repr(user_question),
            flush=True
        )

        print(
            "Model:",
            OPENROUTER_MODEL,
            flush=True
        )

        print(
            "API KEY EXISTS:",
            bool(OPENROUTER_API_KEY),
            flush=True
        )

        print(
            "Context length:",
            len(drug_context or ""),
            flush=True
        )

        print(
            "====================================",
            flush=True
        )

        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )

        print(
            "========== OPENROUTER RESPONSE ==========",
            flush=True
        )

        print(
            "Status code:",
            response.status_code,
            flush=True
        )

        print(
            "Response:",
            response.text[:5000],
            flush=True
        )

        print(
            "==========================================",
            flush=True
        )

        if not response.ok:

            print(
                "OpenRouter error:",
                response.status_code,
                response.text,
                flush=True
            )

            return None

        data = response.json()

        choices = data.get(
            "choices",
            []
        )

        if not choices:

            print(
                "OpenRouter returned no choices",
                flush=True
            )

            return None

        message = choices[0].get(
            "message",
            {}
        )

        answer = message.get(
            "content"
        )

        finish_reason = choices[0].get(
            "finish_reason"
        )

        print(
            "Finish reason:",
            finish_reason,
            flush=True
        )

        print(
            "Answer exists:",
            bool(answer),
            flush=True
        )

        if not answer:

            print(
                "OpenRouter returned empty final answer.",
                flush=True
            )

            return None

        return answer.strip()

    except Exception as e:

        print(
            "OpenRouter exception:",
            repr(e),
            flush=True
        )

        return None
    

# =========================================================
# USER FUNCTIONS
# =========================================================

def get_or_create_user(user):

    bale_user_id = str(

        user.get("id")
        or user.get("user_id")
        or ""

    )

    if not bale_user_id:

        raise ValueError(
            "Bale user ID not found"
        )

    chat_id = str(

        user.get("chat_id")
        or bale_user_id

    )

    display_name = (

        user.get("first_name")
        or user.get("name")
        or user.get("username")
        or ""

    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            # -------------------------------------------------
            # ONLY MAHROO USERS TABLE
            # -------------------------------------------------

            cur.execute("""
                SELECT id
                FROM mahroo_users
                WHERE bale_user_id = %s
            """, (
                bale_user_id,
            ))

            row = cur.fetchone()

            if row:

                user_id = row[0]

                cur.execute("""
                    UPDATE mahroo_users
                    SET chat_id = %s,
                        display_name = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (
                    chat_id,
                    display_name,
                    user_id
                ))

            else:

                cur.execute("""
                    INSERT INTO mahroo_users (
                        bale_user_id,
                        chat_id,
                        display_name
                    )
                    VALUES (
                        %s,
                        %s,
                        %s
                    )
                    RETURNING id
                """, (
                    bale_user_id,
                    chat_id,
                    display_name
                ))

                user_id = cur.fetchone()[0]

            conn.commit()

            return user_id


# =========================================================
# SESSION
# =========================================================

def set_session(
    user_id,
    state,
    data=None
):

    if data is None:

        data = {}

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO mahroo_user_sessions (
                    user_id,
                    state,
                    data,
                    updated_at
                )
                VALUES (
                    %s,
                    %s,
                    %s::jsonb,
                    CURRENT_TIMESTAMP
                )

                ON CONFLICT (user_id)

                DO UPDATE SET
                    state = EXCLUDED.state,
                    data = EXCLUDED.data,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                user_id,
                state,
                json.dumps(data)
            ))

            conn.commit()


def get_session(
    user_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    state,
                    data
                FROM mahroo_user_sessions
                WHERE user_id = %s
            """, (
                user_id,
            ))

            row = cur.fetchone()

            if not row:

                return None, {}

            return (

                row[0],

                row[1] or {}

            )


def clear_session(
    user_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                DELETE FROM mahroo_user_sessions
                WHERE user_id = %s
            """, (
                user_id,
            ))

            conn.commit()


def save_display_name(
    user_id,
    display_name
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE mahroo_users
                SET display_name = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (
                display_name,
                user_id
            ))

            conn.commit()


# =========================================================
# TIME
# =========================================================

def is_valid_time(
    time_text
):

    if not time_text:

        return False

    if not re.match(
        r"^\d{1,2}:\d{2}$",
        time_text.strip()
    ):

        return False

    try:

        hour, minute = map(

            int,

            time_text
            .strip()
            .split(":")

        )

        return (

            0 <= hour <= 23

            and

            0 <= minute <= 59

        )

    except Exception:

        return False


def schedule_to_datetime(
    today,
    scheduled_time
):

    hour, minute = map(

        int,

        scheduled_time.split(":")

    )

    return datetime(

        today.year,

        today.month,

        today.day,

        hour,

        minute,

        tzinfo=IRAN_TZ

    )
def parse_jalali_date(text):
    """
    دریافت تاریخ شمسی به فرمت YYYY/MM/DD
    و تبدیل آن به تاریخ میلادی برای ذخیره در PostgreSQL.
    """

    try:
        text = text.strip()

        parts = text.split("/")

        if len(parts) != 3:
            return None

        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])

        # بررسی منطقی بودن تاریخ شمسی
        jalali_date = jdatetime.date(
            year,
            month,
            day
        )

        # تبدیل به تاریخ میلادی
        return jalali_date.togregorian()

    except Exception as e:

        print(
            "JALALI DATE PARSE ERROR:",
            repr(e),
            flush=True
        )

        return None


def format_jalali_date(gregorian_date):
    """
    تبدیل تاریخ میلادی به تاریخ شمسی برای نمایش به کاربر.
    """
    if not gregorian_date:
        return None

    jalali_date = jdatetime.date.fromgregorian(
        date=gregorian_date
    )

    return jalali_date.strftime("%Y/%m/%d")

# =========================================================
# DRUG DATABASE
#
# IMPORTANT:
# These tables are the MASTER DRUG DATABASE.
# They are intentionally unchanged.
# =========================================================

def clean_drug_text(
    text
):

    if not text:

        return ""

    text = text.strip()

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text


def get_text_values(
    cur,
    table_name,
    generic_rxcui
):

    try:

        cur.execute(

            f"""
            SELECT *
            FROM {table_name}
            WHERE generic_rxcui = %s
            LIMIT 100;
            """,

            (
                generic_rxcui,
            )

        )

        rows = cur.fetchall()

        columns = [

            desc.name

            for desc
            in cur.description

        ]

        values = []

        for row in rows:

            parts = []

            for index, value in enumerate(row):

                column = columns[index]

                if column == "id":

                    continue

                if value is None:

                    continue

                if isinstance(
                    value,
                    (int, float, bool)
                ):

                    continue

                value = str(
                    value
                ).strip()

                if not value:

                    continue

                parts.append(
                    value
                )

            if parts:

                values.append(
                    " | ".join(parts)
                )

        return values[:20]

    except Exception as e:

        print(
            f"Drug table read error {table_name}:",
            repr(e)
        )

        return []


def search_drug_database(
    drug_name
):

    drug_name = clean_drug_text(
        drug_name
    )

    if not drug_name:

        return None

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            # =================================================
            # EXACT MATCH
            # =================================================

            cur.execute("""
                SELECT
                    generic_rxcui,
                    generic_tty,
                    generic_name
                FROM drugs
                WHERE LOWER(TRIM(generic_name))
                      = LOWER(TRIM(%s))
                LIMIT 1;
            """, (
                drug_name,
            ))

            row = cur.fetchone()

            # =================================================
            # PARTIAL MATCH
            # =================================================

            if not row:

                cur.execute("""
                    SELECT
                        generic_rxcui,
                        generic_tty,
                        generic_name
                    FROM drugs
                    WHERE generic_name ILIKE %s
                    ORDER BY LENGTH(generic_name)
                    LIMIT 1;
                """, (
                    f"%{drug_name}%",
                ))

                row = cur.fetchone()

            if not row:

                return None

            generic_rxcui = row[0]
            generic_tty = row[1]
            generic_name = row[2]

            # =================================================
            # PRODUCTS
            # =================================================

            cur.execute("""
                SELECT
                    product_rxcui,
                    product_name
                FROM drug_products
                WHERE generic_rxcui = %s
                ORDER BY product_name
                LIMIT 10;
            """, (
                generic_rxcui,
            ))

            products = cur.fetchall()

            product_list = []

            for product in products:

                product_list.append({

                    "product_rxcui":
                        product[0],

                    "product_name":
                        product[1]

                })

            # =================================================
            # CHILD TABLES
            # =================================================

            indications = get_text_values(

                cur,

                "drug_indications",

                generic_rxcui

            )

            side_effects = get_text_values(

                cur,

                "drug_side_effects",

                generic_rxcui

            )

            contraindications = get_text_values(

                cur,

                "drug_contraindications",

                generic_rxcui

            )

            warnings = get_text_values(

                cur,

                "drug_warnings",

                generic_rxcui

            )

            precautions = get_text_values(

                cur,

                "drug_precautions",

                generic_rxcui

            )

            interactions = get_text_values(

                cur,

                "drug_interactions",

                generic_rxcui

            )

            return {

                "generic_rxcui":
                    generic_rxcui,

                "generic_tty":
                    generic_tty,

                "generic_name":
                    generic_name,

                "products":
                    product_list,

                "indications":
                    indications,

                "side_effects":
                    side_effects,

                "contraindications":
                    contraindications,

                "warnings":
                    warnings,

                "precautions":
                    precautions,

                "interactions":
                    interactions

            }


# =========================================================
# BUILD DRUG CONTEXT FOR LLM
# =========================================================

def drug_to_context(
    drug
):

    if not drug:

        return ""

    context = []

    context.append(

        f"نام ژنریک: "
        f"{drug.get('generic_name', '')}"

    )

    context.append(

        f"RxCUI: "
        f"{drug.get('generic_rxcui', '')}"

    )

    context.append(

        f"نوع: "
        f"{drug.get('generic_tty', '')}"

    )

    products = drug.get(
        "products",
        []
    )

    if products:

        product_text = []

        for item in products:

            name = item.get(
                "product_name"
            )

            if name:

                product_text.append(
                    str(name)
                )

        if product_text:

            context.append(

                "فرآورده‌ها: "

                + "؛ ".join(
                    product_text
                )

            )

    sections = [

        (
            "موارد مصرف",
            "indications"
        ),

        (
            "عوارض جانبی",
            "side_effects"
        ),

        (
            "موارد منع مصرف",
            "contraindications"
        ),

        (
            "هشدارها",
            "warnings"
        ),

        (
            "احتیاط‌ها",
            "precautions"
        ),

        (
            "تداخلات",
            "interactions"
        )

    ]

    for title, key in sections:

        values = drug.get(
            key,
            []
        )

        if not values:

            continue

        context.append(

            f"{title}:\n"

            + "\n".join(

                f"- {value}"

                for value
                in values[:10]

            )

        )

    return "\n\n".join(
        context
    )

# =========================================================
# GET ALL DRUG NAMES FROM DATABASE
# =========================================================

def get_all_drug_names():

    try:

        with get_db_connection() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT DISTINCT generic_name
                    FROM drugs
                    WHERE generic_name IS NOT NULL
                      AND TRIM(generic_name) <> ''
                    ORDER BY LENGTH(generic_name) DESC;
                    """
                )

                rows = cur.fetchall()

        drug_names = [
            str(row[0]).strip()
            for row in rows
            if row[0]
        ]

        print(
            "Total generic drug names loaded from DB:",
            len(drug_names),
            flush=True
        )

        return drug_names

    except Exception as e:

        print(
            "Error loading drug names from DB:",
            repr(e),
            flush=True
        )

        return []


# =========================================================
# FIND DATABASE DRUG NAMES INSIDE QUESTION
# =========================================================

def find_direct_drug_names_in_question(question):

    question_normalized = clean_drug_text(question).lower()

    if not question_normalized:
        return []

    drug_names = get_all_drug_names()

    found = []

    for drug_name in drug_names:

        drug_normalized = clean_drug_text(
            str(drug_name)
        ).lower()

        if not drug_normalized:
            continue

        if drug_normalized in question_normalized:

            found.append(drug_name)

    # Longer names first
    found = sorted(
        set(found),
        key=lambda x: len(str(x)),
        reverse=True
    )

    print(
        "Direct DB drug matches:",
        found,
        flush=True
    )

    return found


# =========================================================
# EXTRACT PERSIAN DRUG CANDIDATES
# =========================================================

def extract_persian_candidates(question):

    import re

    question = clean_drug_text(question)

    if not question:
        return []

    # Persian word sequences
    words = re.findall(
        r'[\u0600-\u06FF]+',
        question
    )

    # Common Persian words that should NEVER
    # be sent to the drug resolver
    stop_words = {
        "ایا",
        "آیا",
        "می",
        "میتونم",
        "میتوانم",
        "میشه",
        "میشود",
        "میشه",
        "با",
        "و",
        "را",
        "رو",
        "برای",
        "چی",
        "چه",
        "خوب",
        "خوبه",
        "است",
        "هست",
        "هستند",
        "کنم",
        "کنم",
        "مصرف",
        "کنار",
        "هم",
        "باهم",
        "دارو",
        "دارویی",
        "تداخل",
        "عوارض",
        "باعث",
        "آیا",
        "من",
        "این",
        "آن",
        "از",
        "در",
        "به",
        "که",
        "چطور",
        "چگونه",
        "میتوان",
        "میتواند",
        "شد",
        "شود",
        "دارد",
        "دارم",
        "دارید",
        "بخورم",
        "بخوریم",
        "بخورم",
        "استفاده",
        "کنید",
        "کردن",
        "کرد",
        "کردم",
        "چیه",
        "چیست",
        "میشه",
        "لطفا",
        "لطفاً"
    }

    candidates = []

    # -----------------------------------------------------
    # Single-word candidates
    # -----------------------------------------------------

    for word in words:

        word = word.strip()

        if not word:
            continue

        if word in stop_words:
            continue

        if len(word) < 3:
            continue

        candidates.append(word)

    # -----------------------------------------------------
    # Two-word and three-word candidates
    # -----------------------------------------------------

    filtered_words = [
        word
        for word in words
        if word not in stop_words
        and len(word) >= 2
    ]

    for i in range(len(filtered_words)):

        # Two words
        if i + 1 < len(filtered_words):

            candidate = (
                filtered_words[i]
                + " "
                + filtered_words[i + 1]
            )

            candidates.append(candidate)

        # Three words
        if i + 2 < len(filtered_words):

            candidate = (
                filtered_words[i]
                + " "
                + filtered_words[i + 1]
                + " "
                + filtered_words[i + 2]
            )

            candidates.append(candidate)

    # Remove duplicates
    candidates = list(
        dict.fromkeys(candidates)
    )

    print(
        "Persian drug candidates:",
        candidates,
        flush=True
    )

    return candidates

# =========================================================
# FIND DRUGS IN USER QUESTION
# =========================================================

def find_drugs_in_question(question):

    question = clean_drug_text(question)

    if not question:
        return []

    print(
        "Question drug detection started:",
        question,
        flush=True
    )

    resolved_names = []

    # =====================================================
    # STEP 1
    # Direct matching against ALL database drug names
    # =====================================================

    print(
        "QUESTION DRUG DETECTION STEP 1:",
        "Searching all DB drug names...",
        flush=True
    )

    direct_matches = find_direct_drug_names_in_question(
        question
    )

    for drug_name in direct_matches:

        if drug_name not in resolved_names:

            resolved_names.append(drug_name)

    # =====================================================
    # STEP 2
    # Persian / misspelled / transliterated candidates
    # =====================================================

    print(
        "QUESTION DRUG DETECTION STEP 2:",
        "Searching Persian candidates...",
        flush=True
    )

    persian_candidates = extract_persian_candidates(
        question
    )

    for candidate in persian_candidates:

        # If this candidate is already essentially
        # covered by a direct match, skip it.
        if any(
            candidate.lower() in str(name).lower()
            for name in direct_matches
        ):
            continue

        try:

            print(
                "Trying drug resolver:",
                repr(candidate),
                flush=True
            )

            resolved_name = resolve_drug_name(
                candidate
            )

            print(
                "Resolver result:",
                repr(candidate),
                "->",
                repr(resolved_name),
                flush=True
            )

            if resolved_name:

                if resolved_name not in resolved_names:

                    resolved_names.append(
                        resolved_name
                    )

        except Exception as e:

            print(
                "Resolver error for candidate:",
                repr(candidate),
                repr(e),
                flush=True
            )

    # =====================================================
    # STEP 3
    # Remove duplicate resolved names
    # =====================================================

    resolved_names = list(
        dict.fromkeys(resolved_names)
    )

    print(
        "FINAL RESOLVED DRUG NAMES:",
        resolved_names,
        flush=True
    )

    if not resolved_names:

        print(
            "No drugs detected in question.",
            flush=True
        )

        return []

    # =====================================================
    # STEP 4
    # Retrieve actual drug information from DB
    # =====================================================

    found_drugs = []

    for drug_name in resolved_names:

        try:

            print(
                "Searching database for:",
                repr(drug_name),
                flush=True
            )

            drug = search_drug_database(
                drug_name
            )

            if drug:

                found_drugs.append(drug)

                print(
                    "Drug information found:",
                    repr(drug_name),
                    flush=True
                )

            else:

                print(
                    "No database information found for:",
                    repr(drug_name),
                    flush=True
                )

        except Exception as e:

            print(
                "Drug database search error:",
                repr(drug_name),
                repr(e),
                flush=True
            )

    # =====================================================
    # STEP 5
    # Remove duplicate drugs using RxCUI
    # =====================================================

    unique_drugs = []

    seen_rxcui = set()

    for drug in found_drugs:

        rxcui = drug.get(
            "generic_rxcui"
        )

        if rxcui:

            if rxcui in seen_rxcui:
                continue

            seen_rxcui.add(rxcui)

        unique_drugs.append(drug)

    print(
        "Total unique drugs found:",
        len(unique_drugs),
        flush=True
    )

    return unique_drugs
# =========================================================
# FORMAT DRUG RESULT
# =========================================================

def format_drug_result(
    drug
):

    if not drug:

        return (
            "❌ اطلاعاتی برای این دارو پیدا نشد."
        )

    # =====================================================
    # BASIC DATABASE INFORMATION
    # =====================================================

    generic_name = drug.get(
        "generic_name",
        "-"
    )

    generic_tty = drug.get(
        "generic_tty",
        "-"
    )

    generic_rxcui = drug.get(
        "generic_rxcui",
        "-"
    )

    # =====================================================
    # PRODUCTS
    # =====================================================

    products = drug.get(
        "products",
        []
    )

    product_names = []

    for product in products[:8]:

        if not isinstance(product, dict):
            continue

        name = product.get(
            "product_name"
        )

        if name:

            product_names.append(
                str(name).strip()
            )

    # =====================================================
    # MEDICAL INFORMATION
    # =====================================================

    sections = [

        (
            "Indications",
            "indications",
            "🩺 موارد مصرف"
        ),

        (
            "Side effects",
            "side_effects",
            "⚠️ عوارض جانبی"
        ),

        (
            "Contraindications",
            "contraindications",
            "🚫 موارد منع مصرف"
        ),

        (
            "Warnings",
            "warnings",
            "⚠️ هشدارها"
        ),

        (
            "Precautions",
            "precautions",
            "ℹ️ احتیاط‌ها"
        ),

        (
            "Interactions",
            "interactions",
            "🔄 تداخلات دارویی"
        )
    ]

    medical_parts = []

    for english_title, key, persian_title in sections:

        values = drug.get(
            key,
            []
        )

        if not values:
            continue

        cleaned_values = []

        for value in values[:6]:

            if value is None:
                continue

            value = str(value).strip()

            if not value:
                continue

            # Prevent extremely long FDA text
            # from being sent to the model.
            if len(value) > 1500:

                value = (
                    value[:1500]
                    + "..."
                )

            cleaned_values.append(
                value
            )

        if cleaned_values:

            medical_parts.append(
                f"{english_title}:\n"
                + "\n".join(
                    f"- {value}"
                    for value in cleaned_values
                )
            )

    # =====================================================
    # BUILD DATABASE CONTEXT
    # =====================================================

    context_parts = []

    context_parts.append(
        f"Generic drug name: {generic_name}"
    )

    if generic_tty:

        context_parts.append(
            f"Drug type: {generic_tty}"
        )

    if generic_rxcui:

        context_parts.append(
            f"RxCUI: {generic_rxcui}"
        )

    if product_names:

        context_parts.append(
            "Products:\n"
            + "\n".join(
                f"- {name}"
                for name in product_names
            )
        )

    if medical_parts:

        context_parts.extend(
            medical_parts
        )

    database_context = "\n\n".join(
        context_parts
    )

    # =====================================================
    # AI TRANSLATION / SIMPLIFICATION
    # =====================================================

    translation_prompt = f"""
You are the Persian medical information formatter for the Mahroo
medication information bot.

The following information has been retrieved directly from a
structured drug database.

Your task is to translate and organize this information into
clear, natural and understandable Persian for a general user.

IMPORTANT RULES:

1. The provided database information is the ONLY medical source.

2. Do NOT add any medical information that is not present in the
   provided text.

3. Do NOT invent indications, side effects, contraindications,
   warnings, precautions or drug interactions.

4. Do NOT diagnose the user.

5. Do NOT tell the user whether they personally should take the drug.

6. Do NOT prescribe or recommend a dose.

7. Do NOT change numerical values, strengths, units or technical
   identifiers.

8. Keep the meaning of the original medical information unchanged.

9. Translate medical terminology accurately but use simple,
   natural Persian whenever possible.

10. Avoid word-for-word translation when it produces unnatural
    Persian. Translate the meaning naturally.

11. Do not turn general information into a personal medical
    recommendation.

12. If the original information is uncertain, conditional or
    limited, preserve that uncertainty.

13. Do not invent missing information.

14. Remove unnecessary legal or regulatory wording if it does not
    contain useful medical information.

15. Avoid repeating the same information.

16. Keep the final response reasonably short and readable.

17. Do not use Markdown tables.

18. Do not mention AI, OpenRouter, prompts or these instructions.

19. Do not say that you searched the internet.

20. Return ONLY the final Persian response.

OUTPUT FORMAT:

💊 اطلاعات دارو

نام دارو: [نام رایج فارسی دارو]

📦 فرآورده‌ها
• [در صورت وجود]

🩺 موارد مصرف
• [اطلاعات موجود در منبع]

⚠️ عوارض جانبی
• [اطلاعات موجود در منبع]

🚫 موارد منع مصرف
• [اطلاعات موجود در منبع]

⚠️ هشدارها
• [اطلاعات موجود در منبع]

ℹ️ احتیاط‌ها
• [اطلاعات موجود در منبع]

🔄 تداخلات دارویی
• [اطلاعات موجود در منبع]

IMPORTANT:
Only include sections for which information actually exists
in the database.

At the end, add exactly:

ℹ️ این اطلاعات برای آگاهی عمومی است و جایگزین توصیه پزشک یا
داروساز نیست.

DATABASE INFORMATION:

{database_context}
"""

    try:

        print(
            "Calling AI to translate drug information to Persian...",
            flush=True
        )

        translated_text = ask_llm(
            translation_prompt
        )

        if translated_text:

            translated_text = (
                translated_text
                .replace("```text", "")
                .replace("```markdown", "")
                .replace("```", "")
                .strip()
            )

            if translated_text:

                print(
                    "Drug information translated successfully.",
                    flush=True
                )

                return translated_text

        print(
            "AI returned an empty translation.",
            flush=True
        )

    except Exception as e:

        print(
            "Drug information translation error:",
            repr(e),
            flush=True
        )

        import traceback

        traceback.print_exc()

    # =====================================================
    # FALLBACK
    # =====================================================

    print(
        "Using database information as fallback.",
        flush=True
    )

    lines = []

    lines.append(
        "💊 اطلاعات دارو"
    )

    lines.append("")

    lines.append(
        f"💊 نام دارو: {generic_name}"
    )

    if generic_rxcui:

        lines.append(
            f"🔢 RxCUI: {generic_rxcui}"
        )

    if product_names:

        lines.append("")

        lines.append(
            "📦 فرآورده‌ها:"
        )

        for name in product_names:

            lines.append(
                f"• {name}"
            )

    for english_title, key, persian_title in sections:

        values = drug.get(
            key,
            []
        )

        if not values:
            continue

        lines.append("")

        lines.append(
            persian_title
        )

        for value in values[:6]:

            if value:

                lines.append(
                    f"• {value}"
                )

    lines.append("")

    lines.append(
        "ℹ️ این اطلاعات برای آگاهی عمومی است و جایگزین توصیه پزشک یا "
        "داروساز نیست."
    )

    return "\n".join(
        lines
    )




# =========================================================
# MAIN MENU
# =========================================================

def main_menu(
    chat_id,
    user_id
):

    set_session(
        user_id,
        "MAIN_MENU"
    )

    send_message(

        chat_id,

        "🌷 به مهرو خوش آمدید.\n\n"
        "از منوی پایین انتخاب کنید.",

        MAIN_MENU_BUTTONS
    )


# =========================================================
# START
# =========================================================

def start_conversation(
    chat_id,
    user_id
):

    # /start ALWAYS opens the main menu.

    main_menu(
        chat_id,
        user_id
    )


# =========================================================
# CANCEL
# =========================================================

def cancel_conversation(
    chat_id,
    user_id
):

    clear_session(
        user_id
    )

    set_session(
        user_id,
        "MAIN_MENU"
    )

    send_message(

        chat_id,

        "عملیات لغو شد.",

        MAIN_MENU_BUTTONS
    )


# =========================================================
# DRUG SEARCH
# =========================================================

def start_drug_search(
    chat_id,
    user_id
):

    set_session(
        user_id,
        "ASK_DRUG_SEARCH"
    )

    send_message(

        chat_id,

        "🔎 نام دارو را وارد کنید.\n\n"
        "مثلاً:\n"
        "• paracetamol\n"
        "• ibuprofen\n"
        "• acetaminophen\n\n"
        "نام دارو را همینجا بنویسید.",

        MAIN_MENU_BUTTONS
    )


# =========================================================
# AI DRUG QUESTION
# =========================================================

def start_drug_question(
    chat_id,
    user_id
):

    set_session(
        user_id,
        "ASK_DRUG_QUESTION"
    )

    send_message(

        chat_id,

        "💬 حالت سؤال دارویی فعال شد.\n\n"
        "سؤال خود را بنویسید.\n\n"
        "مثلاً:\n"
        "• پاراستامول برای چی خوبه؟\n"
        "• عوارض ایبوپروفن چیه؟\n"
        "• تداخلات وارفارین چیست؟\n"
        "• آیا این دارو باعث خواب‌آلودگی می‌شود؟\n\n"
        "🤖 سؤال شما برای دستیار هوشمند مهرو ارسال می‌شود.",

        MAIN_MENU_BUTTONS
    )


# =========================================================
# MEDICATION FUNCTIONS
# =========================================================

def save_medication(
    user_id,
    name,
    doses_per_day,
    number_of_doses,
    times,
    start_date=None,
    end_date=None
):
    with get_db_connection() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO mahroo_medications (
                    user_id,
                    name,
                    doses_per_day,
                    number_of_doses,
                    active,
                    start_date,
                    end_date
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE,
                    %s,
                    %s
                )
                RETURNING id
            """, (
                user_id,
                name,
                doses_per_day,
                number_of_doses,
                start_date,
                end_date
            ))

            medication_id = cur.fetchone()[0]

            for scheduled_time in times:
                cur.execute("""
                    INSERT INTO mahroo_medication_schedules (
                        medication_id,
                        scheduled_time,
                        active
                    )
                    VALUES (
                        %s,
                        %s,
                        TRUE
                    )
                """, (
                    medication_id,
                    scheduled_time
                ))

            conn.commit()

    return medication_id

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            # -------------------------------------------------
            # NEW MAHROO MEDICATION TABLE
            # -------------------------------------------------

            cur.execute("""
                INSERT INTO mahroo_medications (
                    user_id,
                    name,
                    doses_per_day,
                    number_of_doses,
                    active,
                    start_date,
                    end_date
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE,
                    %s,
                    %s
                )
                RETURNING id
            """, (
                user_id,
                name,
                doses_per_day,
                number_of_doses,
                start_date,
                end_date
            ))

            medication_id = (
                cur.fetchone()[0]
            )

            # -------------------------------------------------
            # NEW MAHROO SCHEDULE TABLE
            # -------------------------------------------------

            for scheduled_time in times:

                cur.execute("""
                    INSERT INTO mahroo_medication_schedules (
                        medication_id,
                        scheduled_time,
                        active
                    )
                    VALUES (
                        %s,
                        %s,
                        TRUE
                    )
                """, (
                    medication_id,
                    scheduled_time
                ))

            conn.commit()

            return medication_id


def get_user_medications(
    user_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    name,
                    doses_per_day,
                    number_of_doses,
                    active
                FROM mahroo_medications
                WHERE user_id = %s
                ORDER BY id DESC
            """, (
                user_id,
            ))

            return cur.fetchall()


def get_medication_by_button(
    user_id,
    text
):

    if not text.startswith("💊"):

        return None

    name = text[2:].strip()

    if not name:

        return None

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    name,
                    doses_per_day,
                    number_of_doses,
                    active
                FROM mahroo_medications
                WHERE user_id = %s
                  AND name = %s
                LIMIT 1
            """, (
                user_id,
                name
            ))

            return cur.fetchone()


def show_medications(
    chat_id,
    user_id
):

    medications = get_user_medications(
        user_id
    )

    if not medications:

        send_message(

            chat_id,

            "💊 هنوز دارویی ثبت نکرده‌اید.",

            MAIN_MENU_BUTTONS
        )

        set_session(
            user_id,
            "MAIN_MENU"
        )

        return

    buttons = []

    for medication in medications:

        name = medication[1]

        active = medication[4]

        if active:

            buttons.append([
                f"💊 {name}"
            ])

    # Permanent menu is added by send_message.
    send_message(

        chat_id,

        "💊 داروهای فعال شما:",

        buttons
    )

    set_session(
        user_id,
        "MEDICATION_LIST"
    )


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
                    doses_per_day,
                    number_of_doses,
                    active
                FROM mahroo_medications
                WHERE id = %s
                  AND user_id = %s
            """, (
                medication_id,
                user_id
            ))

            medication = cur.fetchone()

            if not medication:

                send_message(

                    chat_id,

                    "دارو پیدا نشد.",

                    MAIN_MENU_BUTTONS
                )

                return

            cur.execute("""
                SELECT scheduled_time
                FROM mahroo_medication_schedules
                WHERE medication_id = %s
                  AND active = TRUE
                ORDER BY scheduled_time
            """, (
                medication_id,
            ))

            schedules = cur.fetchall()

    name = medication[1]

    times = [

        row[0]

        for row in schedules

    ]

    text = (

        f"💊 {name}\n\n"

        f"تعداد دفعات مصرف: "
        f"{medication[2] or '-'}\n"

        f"تعداد کل دوز: "
        f"{medication[3] or '-'}\n\n"

        "⏰ زمان‌های مصرف:\n"

    )

    if times:

        text += "\n".join(

            f"• {time}"

            for time in times

        )

    else:

        text += "ثبت نشده"

    buttons = [

        ["✏️ تغییر زمان مصرف"],

        ["🗑 حذف دارو"],

        ["↩️ داروهای من"]

    ]

    send_message(

        chat_id,

        text,

        buttons
    )

    set_session(

        user_id,

        "MEDICATION_MANAGEMENT",

        {
            "medication_id":
                medication_id
        }
    )


# =========================================================
# REMINDER BUTTONS
# =========================================================

REMINDER_TAKEN = (
    "✅ مصرف کردم"
)

REMINDER_SNOOZE = (
    "⏰ ۵ دقیقه بعد یادآوری کن"
)

REMINDER_NOT_TAKEN = (
    "❌ مصرف نکردم"
)


# =========================================================
# REMINDER FUNCTIONS
# =========================================================

def get_active_reminder(
    user_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    medication_id,
                    schedule_id,
                    scheduled_for,
                    status
                FROM mahroo_reminder_occurrences
                WHERE user_id = %s
                  AND status IN (
                      'sent',
                      'snoozed'
                  )
                ORDER BY scheduled_for DESC
                LIMIT 1
            """, (
                user_id,
            ))

            return cur.fetchone()


def mark_reminder_taken(
    reminder_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE mahroo_reminder_occurrences
                SET status = 'taken',
                    taken_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND status IN (
                      'sent',
                      'snoozed'
                  )
            """, (
                reminder_id,
            ))

            conn.commit()


def mark_reminder_not_taken(
    reminder_id
):

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                UPDATE mahroo_reminder_occurrences
                SET status = 'not_taken',
                    not_taken_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND status IN (
                      'sent',
                      'snoozed'
                  )
            """, (
                reminder_id,
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
                UPDATE mahroo_reminder_occurrences
                SET status = 'snoozed',
                    snoozed_until = %s
                WHERE id = %s
                  AND status = 'sent'
            """, (
                snooze_until,
                reminder_id
            ))

            conn.commit()


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

    # =====================================================
    # TAKEN
    # =====================================================

    if text == REMINDER_TAKEN:

        mark_reminder_taken(
            reminder_id
        )

        send_message(

            chat_id,

            "✅ مصرف دارو ثبت شد.",

            MAIN_MENU_BUTTONS
        )

        return True

    # =====================================================
    # NOT TAKEN
    # =====================================================

    if text == REMINDER_NOT_TAKEN:

        mark_reminder_not_taken(
            reminder_id
        )

        send_message(

            chat_id,

            "ثبت شد. امیدواریم مصرف بعدی "
            "را به‌موقع انجام دهید.",

            MAIN_MENU_BUTTONS
        )

        return True

    # =====================================================
    # SNOOZE
    # =====================================================

    if text == REMINDER_SNOOZE:

        snooze_reminder(
            reminder_id
        )

        send_message(

            chat_id,

            "⏰ حتماً. ۵ دقیقه دیگر "
            "دوباره یادآوری می‌کنم.",

            MAIN_MENU_BUTTONS
        )

        return True

    return False


# =========================================================
# CREATE DUE OCCURRENCES
# =========================================================

def create_due_occurrences():

    now = datetime.now(
        IRAN_TZ
    )

    today = now.date()

    created = 0

    earliest = (

        now

        - timedelta(
            minutes=
            REMINDER_GRACE_MINUTES
        )

    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            # -------------------------------------------------
            # ONLY MAHROO MEDICATION TABLES
            # -------------------------------------------------

            cur.execute("""
                SELECT
                    m.id,
                    m.user_id,
                    ms.id,
                    ms.scheduled_time
                FROM mahroo_medications m
                JOIN mahroo_medication_schedules ms
                    ON ms.medication_id = m.id
                WHERE m.active = TRUE
                  AND ms.active = TRUE
                  AND (m.start_date IS NULL OR m.start_date <= %s)
                  AND (m.end_date IS NULL OR m.end_date >= %s)
            """, (
                today,
                today
            ))

            rows = cur.fetchall()

            for row in rows:

                medication_id = row[0]

                user_id = row[1]

                schedule_id = row[2]

                scheduled_time = row[3]

                if not is_valid_time(
                    scheduled_time
                ):

                    continue

                scheduled_dt = (
                    schedule_to_datetime(
                        today,
                        scheduled_time
                    )
                )

                # -------------------------------------------------
                # ONLY REMINDERS WITHIN LAST 10 MINUTES
                # -------------------------------------------------

                if not (

                    earliest
                    <= scheduled_dt
                    <= now

                ):

                    continue

                # -------------------------------------------------
                # Prevent duplicate occurrence
                # -------------------------------------------------

                cur.execute("""
                    SELECT id
                    FROM mahroo_reminder_occurrences
                    WHERE schedule_id = %s
                      AND scheduled_for = %s
                    LIMIT 1
                """, (
                    schedule_id,
                    scheduled_dt
                ))

                exists = cur.fetchone()

                if exists:

                    continue

                # -------------------------------------------------
                # Create new reminder
                # -------------------------------------------------

                cur.execute("""
                    INSERT INTO mahroo_reminder_occurrences (
                        medication_id,
                        schedule_id,
                        user_id,
                        scheduled_for,
                        status
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        %s,
                        'pending'
                    )
                """, (
                    medication_id,
                    schedule_id,
                    user_id,
                    scheduled_dt
                ))

                created += 1

            conn.commit()

    return created


# =========================================================
# GET PENDING REMINDERS
# =========================================================

def get_pending_reminders():

    now = datetime.now(
        IRAN_TZ
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    ro.id,
                    ro.user_id,
                    u.chat_id,
                    m.name,
                    ro.scheduled_for,
                    ro.status
                FROM mahroo_reminder_occurrences ro

                JOIN mahroo_users u
                    ON u.id = ro.user_id

                JOIN mahroo_medications m
                    ON m.id = ro.medication_id

                WHERE ro.status = 'pending'

                  AND ro.scheduled_for
                      BETWEEN %s AND %s

                ORDER BY ro.scheduled_for
            """, (

                now
                - timedelta(
                    minutes=
                    REMINDER_GRACE_MINUTES
                ),

                now

            ))

            return cur.fetchall()


# =========================================================
# SEND NORMAL REMINDER
# =========================================================

def send_normal_reminder(
    reminder
):

    reminder_id = reminder[0]

    chat_id = reminder[2]

    medication_name = reminder[3]

    text = (

        f"💊 وقت مصرف داروی "
        f"«{medication_name}» است.\n\n"

        "لطفاً وضعیت مصرف را انتخاب کنید:"

    )

    buttons = [

        [REMINDER_TAKEN],

        [REMINDER_SNOOZE],

        [REMINDER_NOT_TAKEN]

    ]

    response = send_message(

        chat_id,

        text,

        buttons
    )

    # -----------------------------------------------------
    # Only mark as sent if Bale accepted the message.
    # -----------------------------------------------------

    if response and response.ok:

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                cur.execute("""
                    UPDATE mahroo_reminder_occurrences
                    SET status = 'sent',
                        sent_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND status = 'pending'
                """, (
                    reminder_id,
                ))

                conn.commit()

        return True

    print(
        "Reminder was NOT marked as sent:",
        reminder_id
    )

    return False


# =========================================================
# GET SNOOZED REMINDERS
# =========================================================

def get_snoozed_reminders():

    now = datetime.now(
        IRAN_TZ
    )

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    ro.id,
                    ro.user_id,
                    u.chat_id,
                    m.name,
                    ro.snoozed_until,
                    ro.status
                FROM mahroo_reminder_occurrences ro

                JOIN mahroo_users u
                    ON u.id = ro.user_id

                JOIN mahroo_medications m
                    ON m.id = ro.medication_id

                WHERE ro.status = 'snoozed'
                  AND ro.snoozed_until <= %s

                ORDER BY ro.snoozed_until
            """, (
                now,
            ))

            return cur.fetchall()


# =========================================================
# SEND SNOOZED REMINDER
# =========================================================

def send_snoozed_reminder(
    reminder
):

    reminder_id = reminder[0]

    chat_id = reminder[2]

    medication_name = reminder[3]

    text = (

        "⏰ یادآوری مجدد\n\n"

        f"زمان مصرف «{medication_name}» "
        "رسیده است.\n\n"

        "آیا دارو را مصرف کردید؟"

    )

    buttons = [

        [REMINDER_TAKEN],

        [REMINDER_SNOOZE],

        [REMINDER_NOT_TAKEN]

    ]

    response = send_message(

        chat_id,

        text,

        buttons
    )

    if response and response.ok:

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                cur.execute("""
                    UPDATE mahroo_reminder_occurrences
                    SET status = 'sent',
                        sent_at = CURRENT_TIMESTAMP,
                        snoozed_until = NULL
                    WHERE id = %s
                      AND status = 'snoozed'
                """, (
                    reminder_id,
                ))

                conn.commit()

        return True

    print(
        "Snoozed reminder was NOT marked as sent:",
        reminder_id
    )

    return False


# =========================================================
# CHECK REMINDERS
# =========================================================

@app.route(
    "/check-reminders",
    methods=["GET"]
)
def check_reminders():

    try:

        created = (
            create_due_occurrences()
        )

        pending = (
            get_pending_reminders()
        )

        snoozed = (
            get_snoozed_reminders()
        )

        sent = 0

        errors = 0

        # =================================================
        # NORMAL REMINDERS
        # =================================================

        for reminder in pending:

            try:

                success = (
                    send_normal_reminder(
                        reminder
                    )
                )

                if success:

                    sent += 1

                else:

                    errors += 1

            except Exception as e:

                errors += 1

                print(
                    "Normal reminder exception:",
                    repr(e)
                )

        # =================================================
        # SNOOZED REMINDERS
        # =================================================

        for reminder in snoozed:

            try:

                success = (
                    send_snoozed_reminder(
                        reminder
                    )
                )

                if success:

                    sent += 1

                else:

                    errors += 1

            except Exception as e:

                errors += 1

                print(
                    "Snoozed reminder exception:",
                    repr(e)
                )

        return jsonify({

            "status":
                "ok",

            "created":
                created,

            "sent":
                sent,

            "errors":
                errors

        })

    except Exception as e:

        print(
            "check-reminders error:",
            repr(e)
        )

        return jsonify({

            "status":
                "error",

            "message":
                str(e)

        }), 500


# =========================================================
# HOME
# =========================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify({

        "status":
            "ok",

        "service":
            "Mahroo",

        "message":
            "Mahroo backend is running."

    })


# =================================================
# RESOLVE DRUG NAME 
# =================================================
def resolve_drug_name(text):
    """
    Resolve Persian / English / brand / misspelled drug names
    to a validated English generic drug name.
    """

    text = clean_drug_text(text)

    if not text:
        return None

    print(
        "Drug resolver input:",
        text,
        flush=True
    )

    # =================================================
    # STEP 1 — Direct database search
    # =================================================

    try:

        drug = search_drug_database(text)

        if drug:

            print(
                "Drug resolver: direct match found:",
                text,
                flush=True
            )

            return text

    except Exception as e:

        print(
            "Drug resolver direct search error:",
            repr(e),
            flush=True
        )

    # =================================================
    # STEP 2 — Ask LLM to identify generic drug name
    # =================================================

    try:

        prompt = f"""
You are a drug-name normalization system.

Identify the generic English name of the drug in the user input.

The input may be:
- Persian drug name
- English drug name
- brand name
- misspelled drug name

IMPORTANT:
Return ONLY the generic drug name in English.

Examples:

Input: اسپرین
Output: aspirin

Input: آسپرین
Output: aspirin

Input: پاراستامول
Output: acetaminophen

Input: استامینوفن
Output: acetaminophen

Input: ایبوپروفن
Output: ibuprofen

Input: وارفارین
Output: warfarin

Input: Tylenol
Output: acetaminophen

Do not write a sentence.
Do not explain.
Do not use Persian.
Do not include parentheses.
Do not include dosage.
Do not include strength.
Do not include dosage form.
Do not provide alternatives.

If the input is not a drug or cannot be identified confidently, return:

UNKNOWN

User input:
{text}
"""

        response = ask_llm(prompt)

        if not response:

            print(
                "Drug resolver: LLM returned empty response",
                flush=True
            )

            return None

        resolved_name = response.strip()

        print(
            "Drug resolver raw LLM result:",
            resolved_name,
            flush=True
        )

        if not resolved_name:
            return None

        # =================================================
        # STEP 3 — Clean common LLM response patterns
        # =================================================

        import re

        # Remove markdown/code formatting
        resolved_name = resolved_name.replace(
            "```",
            ""
        ).strip()

        # If model returned something like:
        # اسم علمی این دارو آسپرین (Aspirin) است.
        #
        # extract the English text inside parentheses first.

        parentheses_match = re.search(
            r"\(([A-Za-z][A-Za-z0-9\-\s]*)\)",
            resolved_name
        )

        if parentheses_match:

            candidate = parentheses_match.group(
                1
            ).strip()

            if candidate:

                resolved_name = candidate

        else:

            # Try to extract an English drug name
            # from the response.

            english_matches = re.findall(
                r"\b[A-Za-z][A-Za-z0-9\-]*(?:\s+[A-Za-z][A-Za-z0-9\-]*){0,3}\b",
                resolved_name
            )

            if english_matches:

                # Prefer the shortest reasonable English
                # candidate because generic drug names are
                # normally short.

                candidates = [
                    x.strip()
                    for x in english_matches
                    if x.strip()
                ]

                if candidates:

                    resolved_name = candidates[-1]

        resolved_name = resolved_name.strip(
            " \t\n\r.,:;\"'`"
        )

        print(
            "Drug resolver cleaned result:",
            resolved_name,
            flush=True
        )

        if not resolved_name:
            return None

        if resolved_name.upper() == "UNKNOWN":
            return None

        # =================================================
        # STEP 4 — Validate against drugs table
        # =================================================

        with get_db_connection() as conn:

            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT generic_name
                    FROM drugs
                    WHERE LOWER(generic_name)
                          LIKE LOWER(%s)
                    ORDER BY LENGTH(generic_name) ASC
                    LIMIT 1;
                    """,
                    (
                        resolved_name + "%",
                    )
                )

                row = cur.fetchone()

                if not row:

                    print(
                        "Drug resolver: LLM result not found in DB:",
                        resolved_name,
                        flush=True
                    )

                    return None

                canonical_name = row[0]

                print(
                    "Drug resolver: validated DB match:",
                    canonical_name,
                    flush=True
                )

                return resolved_name

    except Exception as e:

        print(
            "Drug resolver LLM error:",
            repr(e),
            flush=True
        )

        return None
# =========================================================
# RECEIVE MESSAGE
# =========================================================

@app.route(
    "/message",
    methods=["POST"]
)
def receive_message():

    try:

        print(
            "========== MESSAGE ROUTE ENTERED ==========",
            flush=True
        )

        # =================================================
        # READ BALE UPDATE
        # =================================================

        print(
            "STEP 1: Reading raw request...",
            flush=True
        )

        raw_data = request.get_data(
            as_text=True
        )

        print(
            "RAW REQUEST DATA:",
            repr(raw_data),
            flush=True
        )

        # =================================================
        # PARSE JSON
        # =================================================

        print(
            "STEP 2: Parsing JSON...",
            flush=True
        )

        data = request.get_json(
            silent=True
        )

        print(
            "PARSED JSON:",
            repr(data),
            flush=True
        )

        if not data:

            print(
                "ERROR: No JSON received",
                flush=True
            )

            return jsonify({
                "status": "ok"
            })

        print(
            "STEP 3: JSON received successfully",
            flush=True
        )

        # =================================================
        # Bale payload
        # =================================================

        message = (

            data.get("message")

            or data.get("result")

            or data

        )

        print(
            "STEP 4: Message object:",
            repr(message),
            flush=True
        )

        if not isinstance(
            message,
            dict
        ):

            print(
                "ERROR: Message is not a dictionary",
                flush=True
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # USER
        # =================================================

        user = (

            message.get("from")

            or message.get("user")

            or {}

        )

        print(
            "STEP 5: User:",
            repr(user),
            flush=True
        )

        # =================================================
        # CHAT
        # =================================================

        chat = (

            message.get("chat")

            or {}

        )

        print(
            "STEP 6: Chat:",
            repr(chat),
            flush=True
        )

        # =================================================
        # TEXT
        # =================================================

        text = (

            message.get("text")

            or message.get("message")

            or ""

        )

        if not isinstance(
            text,
            str
        ):

            text = str(text)

        print(
            "STEP 7: Raw text:",
            repr(text),
            flush=True
        )
        
        try:
        
            text = clean_drug_text(
                text
            )
        
            print(
                "STEP 8: Cleaned text:",
                repr(text),
                flush=True
            )
        
        except Exception as e:
        
            print(
                "========== CLEAN TEXT ERROR ==========",
                flush=True
            )
        
            print(
                "Error type:",
                type(e).__name__,
                flush=True
            )
        
            print(
                "Error:",
                repr(e),
                flush=True
            )
        
            print(
                "Original text:",
                repr(text),
                flush=True
            )
        
            print(
                "=======================================",
                flush=True
            )
        
            return jsonify({
                "status": "ok"
            })
        
        print(
            "Incoming text:",
            repr(text),
            flush=True
        )

        
        # =================================================
        # CHAT ID
        # =================================================
        
        print(
            "STEP 9: Finding chat_id...",
            flush=True
        )
        
        chat_id = (
        
            chat.get("id")
        
            or message.get(
                "chat_id"
            )
        
            or user.get(
                "chat_id"
            )
        
            or user.get("id")
        
        )
        
        print(
            "STEP 10: chat_id found:",
            repr(chat_id),
            flush=True
        )
        
        if chat_id is None:
        
            print(
                "ERROR: chat_id not found",
                flush=True
            )
        
            return jsonify({
                "status": "ok"
            })
        
        chat_id = str(
            chat_id
        )
        
        print(
            "STEP 11: chat_id converted:",
            repr(chat_id),
            flush=True
        )
        
        # =================================================
        # USER
        # =================================================
        
        print(
            "STEP 12: Calling get_or_create_user...",
            flush=True
        )
        
        try:
        
            user_id = get_or_create_user({
        
                "id":
        
                    user.get("id")
        
                    or user.get("user_id")
        
                    or chat_id,
        
                "chat_id":
        
                    chat_id,
        
                "first_name":
        
                    user.get(
                        "first_name",
                        ""
                    ),
        
                "name":
        
                    user.get(
                        "name",
                        ""
                    ),
        
                "username":
        
                    user.get(
                        "username",
                        ""
                    )
        
            })
        
            print(
                "STEP 13: get_or_create_user OK",
                flush=True
            )
        
            print(
                "USER ID:",
                repr(user_id),
                flush=True
            )
        
        except Exception as e:
        
            print(
                "========== GET USER ERROR ==========",
                flush=True
            )
        
            print(
                "Error type:",
                type(e).__name__,
                flush=True
            )
        
            print(
                "Error:",
                repr(e),
                flush=True
            )
        
            print(
                "====================================",
                flush=True
            )
        
            return jsonify({
                "status": "ok"
            })
        
        # =================================================
        # REMINDER BUTTONS
        # =================================================

        if text in [

            REMINDER_TAKEN,

            REMINDER_SNOOZE,

            REMINDER_NOT_TAKEN

        ]:

            handled = (

                handle_reminder_action(

                    chat_id,

                    user_id,

                    text

                )

            )

            if handled:

                return jsonify({
                    "status": "ok"
                })

        # =================================================
        # CANCEL
        # =================================================

        if text in [

            "❌ لغو",

            "لغو",

            "/cancel"

        ]:

            cancel_conversation(

                chat_id,

                user_id

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # START / MAIN MENU
        #
        # IMPORTANT:
        # This MUST be before get_session().
        #
        # Therefore /start works from every state.
        # =================================================

        normalized_text = (
            text.strip().lower()
        )

        if normalized_text in [

            "/start",

            "start",

            "شروع",

            "استارت",

            "↩️ منوی اصلی"

        ]:

            print(
                "START BUTTON/COMMAND RECEIVED"
            )

            start_conversation(

                chat_id,

                user_id

            )

            return jsonify({
                "status": "ok"
            })

        
        # =================================================
        # GET SESSION
        # =================================================
        
        print(
            "STEP 14: Calling get_session...",
            flush=True
        )
        
        try:
        
            state, session_data = (
        
                get_session(
                    user_id
                )
        
            )
        
            print(
                "STEP 15: get_session OK",
                flush=True
            )
        
            print(
                "========== MESSAGE DEBUG ==========",
                flush=True
            )
        
            print(
                "USER ID:",
                repr(user_id),
                flush=True
            )
        
            print(
                "TEXT:",
                repr(text),
                flush=True
            )
        
            print(
                "STATE:",
                repr(state),
                flush=True
            )
        
            print(
                "SESSION DATA:",
                repr(session_data),
                flush=True
            )
        
            print(
                "===================================",
                flush=True
            )
        
        except Exception as e:
        
            print(
                "========== GET SESSION ERROR ==========",
                flush=True
            )
        
            print(
                "Error type:",
                type(e).__name__,
                flush=True
            )
        
            print(
                "Error:",
                repr(e),
                flush=True
            )
        
            print(
                "User ID:",
                repr(user_id),
                flush=True
            )
        
            print(
                "=======================================",
                flush=True
            )
        
            return jsonify({
                "status": "ok"
            })
        
        if not state:
        
            state = "MAIN_MENU"

        # =================================================
        # MAIN MENU:
        # DASHBOARD
        # =================================================

        if text == "📊 داشبورد من":

            medications = (
                get_user_medications(
                    user_id
                )
            )

            active_count = sum(

                1

                for medication
                in medications

                if medication[4]

            )

            send_message(

                chat_id,

                "📊 داشبورد مهرو\n\n"

                f"💊 تعداد داروهای فعال: "
                f"{active_count}\n\n"

                "از منوی پایین می‌توانید "
                "عملیات دیگری انجام دهید.",

                MAIN_MENU_BUTTONS

            )

            set_session(
                user_id,
                "MAIN_MENU"
            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # ADD MEDICATION
        # =================================================

        if text == "➕ افزودن دارو":

            set_session(

                user_id,

                "ASK_MEDICATION_NAME"

            )

            send_message(

                chat_id,

                "💊 نام دارویی که می‌خواهید "
                "به برنامه خود اضافه کنید را وارد کنید.",

                MAIN_MENU_BUTTONS

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # MEDICATION LIST
        # =================================================

        if text == "💊 داروهای من":

            show_medications(

                chat_id,

                user_id

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # DRUG SEARCH
        # =================================================

        if text == "🔎 جستجوی دارو":

            start_drug_search(

                chat_id,

                user_id

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # AI DRUG QUESTION
        # =================================================

        if text == "💬 سؤال دارویی":

            start_drug_question(

                chat_id,

                user_id

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # DRUG SEARCH AGAIN
        # =================================================

        if text == "🔎 جستجوی داروی دیگر":

            start_drug_search(

                chat_id,

                user_id

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # NEW AI QUESTION
        # =================================================

        if text == "💬 سؤال جدید":

            start_drug_question(

                chat_id,

                user_id

            )

            return jsonify({
                "status": "ok"
            })

       # =================================================
        # DRUG SEARCH STATE
        # =================================================
        
        if state == "ASK_DRUG_SEARCH":
        
            print(
                "========== ENTERED DRUG SEARCH STATE ==========",
                flush=True
            )
        
            if not text:
        
                send_message(
                    chat_id,
                    "🔎 لطفاً نام دارو را وارد کنید.",
                    MAIN_MENU_BUTTONS
                )
        
                return jsonify({
                    "status": "ok"
                })
        
            try:
        
                # =================================================
                # STEP 1 — Resolve drug name
                # =================================================
        
                print(
                    "DRUG SEARCH STEP 1: Resolving drug name...",
                    flush=True
                )
        
                resolved_name = resolve_drug_name(text)
        
                print(
                    "DRUG SEARCH STEP 1 RESULT:",
                    repr(resolved_name),
                    flush=True
                )
        
                # =================================================
                # STEP 2 — No drug found
                # =================================================
        
                if not resolved_name:
        
                    print(
                        "DRUG SEARCH: No resolved drug name",
                        flush=True
                    )
        
                    send_message(
                        chat_id,
        
                        f"❌ دارویی با نام "
                        f"«{text}» در پایگاه داده "
                        "پیدا نشد.\n\n"
                        "لطفاً نام دارو را دوباره وارد کنید.",
        
                        MAIN_MENU_BUTTONS
                    )
        
                    set_session(
                        user_id,
                        "ASK_DRUG_SEARCH"
                    )
        
                    return jsonify({
                        "status": "ok"
                    })
        
                # =================================================
                # STEP 3 — Search database
                # =================================================
        
                print(
                    "DRUG SEARCH STEP 2: Calling search_drug_database...",
                    flush=True
                )
        
                print(
                    "Search term:",
                    repr(resolved_name),
                    flush=True
                )
        
                drug = search_drug_database(
                    resolved_name
                )
        
                print(
                    "DRUG SEARCH STEP 2 RESULT:",
                    repr(drug),
                    flush=True
                )
        
                # =================================================
                # STEP 4 — No database result
                # =================================================
        
                if not drug:
        
                    print(
                        "DRUG SEARCH: Database returned no drug",
                        flush=True
                    )
        
                    send_message(
                        chat_id,
        
                        f"❌ دارویی با نام "
                        f"«{text}» در پایگاه داده "
                        "پیدا نشد.\n\n"
                        "لطفاً نام دارو را دوباره وارد کنید.",
        
                        MAIN_MENU_BUTTONS
                    )
        
                    set_session(
                        user_id,
                        "ASK_DRUG_SEARCH"
                    )
        
                    return jsonify({
                        "status": "ok"
                    })
        
                # =================================================
                # STEP 5 — Format result
                # =================================================
        
                print(
                    "DRUG SEARCH STEP 3: Formatting result...",
                    flush=True
                )
        
                result_text = format_drug_result(
                    drug
                )
        
                print(
                    "DRUG SEARCH STEP 3 RESULT LENGTH:",
                    len(result_text) if result_text else 0,
                    flush=True
                )
        
                # =================================================
                # STEP 6 — Send result to Bale
                # =================================================
        
                print(
                    "DRUG SEARCH STEP 4: Sending result to Bale...",
                    flush=True
                )
        
                send_message(
                    chat_id,
                    result_text,
                    MAIN_MENU_BUTTONS
                )
        
                print(
                    "DRUG SEARCH STEP 4: Bale send_message completed",
                    flush=True
                )
        
                # =================================================
                # STEP 7 — Stay in search mode
                # =================================================
        
                set_session(
                    user_id,
                    "ASK_DRUG_SEARCH"
                )
        
                print(
                    "========== DRUG SEARCH COMPLETED ==========",
                    flush=True
                )
        
                return jsonify({
                    "status": "ok"
                })
        
            except Exception as e:
        
                print(
                    "========== DRUG SEARCH ERROR ==========",
                    flush=True
                )
        
                print(
                    "Drug search error:",
                    repr(e),
                    flush=True
                )
        
                import traceback
        
                traceback.print_exc()
        
                send_message(
                    chat_id,
        
                    "❌ هنگام جستجوی دارو "
                    "خطایی رخ داد.\n\n"
                    "لطفاً دوباره تلاش کنید.",
        
                    MAIN_MENU_BUTTONS
                )
        
                set_session(
                    user_id,
                    "ASK_DRUG_SEARCH"
                )
        
                return jsonify({
                    "status": "ok"
                })



        # =================================================
        # ASK START DATE
        # =================================================
        
        if state == "ASK_START_DATE":
        
            start_date = parse_jalali_date(text)
        
            if start_date is None:
        
                send_message(
                    chat_id,
                    "❌ تاریخ واردشده معتبر نیست.\n\n"
                    "لطفاً تاریخ را به فرمت زیر وارد کنید:\n"
                    "YYYY/MM/DD\n\n"
                    "مثلاً: 1405/06/20",
                    MAIN_MENU_BUTTONS
                )
        
                return jsonify({
                    "status": "ok"
                })
        
            session_data[
                "start_date"
            ] = start_date
        
            set_session(
                user_id,
                "ASK_END_DATE",
                session_data
            )
        
            send_message(
                chat_id,
                "📅 تاریخ پایان مصرف دارو را وارد کنید.\n\n"
                "لطفاً تاریخ را به صورت زیر وارد کنید:\n"
                "YYYY/MM/DD\n\n"
                "مثلاً: 1405/07/20",
                MAIN_MENU_BUTTONS
            )
        
            return jsonify({
                "status": "ok"
            })

        # =================================================
        # ASK END DATE
        # =================================================
        
        if state == "ASK_END_DATE":
        
            end_date = parse_jalali_date(text)
        
            if end_date is None:
        
                send_message(
                    chat_id,
                    "❌ تاریخ واردشده معتبر نیست.\n\n"
                    "لطفاً تاریخ را به فرمت زیر وارد کنید:\n"
                    "YYYY/MM/DD\n\n"
                    "مثلاً: 1405/07/20",
                    MAIN_MENU_BUTTONS
                )
        
                return jsonify({
                    "status": "ok"
                })
        
            start_date = session_data.get(
                "start_date"
            )
        
            if start_date is None:
        
                send_message(
                    chat_id,
                    "❌ تاریخ شروع مصرف ثبت نشده است.\n"
                    "لطفاً دوباره تاریخ شروع را وارد کنید.",
                    MAIN_MENU_BUTTONS
                )
        
                set_session(
                    user_id,
                    "ASK_START_DATE",
                    session_data
                )
        
                return jsonify({
                    "status": "ok"
                })
        
            if end_date < start_date:
        
                send_message(
                    chat_id,
                    "❌ تاریخ پایان نمی‌تواند قبل از تاریخ شروع باشد.\n\n"
                    "لطفاً تاریخ پایان را دوباره وارد کنید.",
                    MAIN_MENU_BUTTONS
                )
        
                return jsonify({
                    "status": "ok"
                })
        
            session_data[
                "end_date"
            ] = end_date
        
            times_text = "\n".join(
                f"• {time}"
                for time in session_data["times"]
            )
        
            start_date_text = format_jalali_date(
                session_data["start_date"]
            )
        
            end_date_text = format_jalali_date(
                session_data["end_date"]
            )
        
            set_session(
                user_id,
                "CONFIRM_MEDICATION",
                session_data
            )
        
            send_message(
                chat_id,
                "💊 اطلاعات دارو:\n\n"
                f"نام: "
                f"{session_data['medication_name']}\n"
                f"دفعات روزانه: "
                f"{session_data['doses_per_day']}\n"
                f"تعداد دوز: "
                f"{session_data['number_of_doses']}\n\n"
                f"⏰ زمان‌ها:\n"
                f"{times_text}\n\n"
                f"📅 تاریخ شروع: "
                f"{start_date_text}\n"
                f"📅 تاریخ پایان: "
                f"{end_date_text}\n\n"
                "آیا اطلاعات صحیح است؟",
                [
                    ["✅ ثبت دارو"],
                    ["❌ لغو"]
                ]
            )
        
            return jsonify({
                "status": "ok"
            })
        # =================================================
        # AI DRUG QUESTION STATE
        # =================================================
        
        print(
            "========== BEFORE AI STATE CHECK ==========",
            flush=True
        )
        
        print(
            "STATE BEFORE AI CHECK:",
            repr(state),
            flush=True
        )
        
        print(
            "STATE TYPE:",
            type(state).__name__,
            flush=True
        )
        
        if state == "ASK_DRUG_QUESTION":
        
            print(
                "========== AI STATE MATCHED ==========",
                flush=True
            )
        
            print(
                "STATE:",
                repr(state),
                flush=True
            )
        
            print(
                "TEXT:",
                repr(text),
                flush=True
            )
        
            if not text:
        
                send_message(
                    chat_id,
                    "💬 لطفاً سؤال دارویی خود را بنویسید.",
                    MAIN_MENU_BUTTONS
                )
        
                return jsonify({
                    "status": "ok"
                })
        
            print(
                "========================================",
                flush=True
            )
        
            print(
                "AI QUESTION RECEIVED",
                flush=True
            )
        
            print(
                "Question:",
                repr(text),
                flush=True
            )
        
            print(
                "User ID:",
                user_id,
                flush=True
            )
        
            print(
                "========================================",
                flush=True
            )
        
            try:
        
                # -------------------------------------------------
                # STEP 1: FIND DRUGS
                # -------------------------------------------------
        
                print(
                    "AI STEP 1: Finding drugs...",
                    flush=True
                )
        
                drugs = find_drugs_in_question(
                    text
                )
        
                print(
                    "AI STEP 1 OK",
                    flush=True
                )
        
                print(
                    "Detected drugs:",
                    [
                        d.get("generic_name")
                        for d in drugs
                    ],
                    flush=True
                )
        
                # -------------------------------------------------
                # STEP 2: BUILD CONTEXT
                # -------------------------------------------------
        
                print(
                    "AI STEP 2: Building drug context...",
                    flush=True
                )
        
                contexts = []
        
                for drug in drugs[:3]:
        
                    print(
                        "Building context for:",
                        drug.get("generic_name"),
                        flush=True
                    )
        
                    context = drug_to_context(
                        drug
                    )
        
                    if context:
        
                        contexts.append(
                            context
                        )
        
                drug_context = (
                    "\n\n"
                    "===================="
                    "\n\n"
                ).join(
                    contexts
                )
        
                print(
                    "AI STEP 2 OK",
                    flush=True
                )
        
                print(
                    "Drug context length:",
                    len(drug_context),
                    flush=True
                )
        
                # -------------------------------------------------
                # STEP 3: CALL LLM
                # -------------------------------------------------
        
                print(
                    "AI STEP 3: Calling OpenRouter...",
                    flush=True
                )
        
                answer = ask_llm(
                    user_question=text,
                    drug_context=drug_context
                )
        
                print(
                    "AI STEP 3 COMPLETED",
                    flush=True
                )
        
                # -------------------------------------------------
                # LLM ERROR
                # -------------------------------------------------
        
                if not answer:
        
                    print(
                        "AI ERROR: ask_llm returned None/empty",
                        flush=True
                    )
        
                    send_message(
                        chat_id,
                        "❌ متأسفانه در دریافت پاسخ "
                        "از دستیار هوشمند مشکلی پیش آمد.\n\n"
                        "لطفاً چند لحظه بعد دوباره تلاش کنید.",
                        MAIN_MENU_BUTTONS
                    )
        
                    set_session(
                        user_id,
                        "ASK_DRUG_QUESTION"
                    )
        
                    return jsonify({
                        "status": "ok"
                    })
        
                # -------------------------------------------------
                # ANSWER
                # -------------------------------------------------
        
                print(
                    "AI STEP 4: Sending answer...",
                    flush=True
                )
        
                answer_text = (
                    "🤖 پاسخ مهرو:\n\n"
                    + answer
                )
        
                send_message(
                    chat_id,
                    answer_text,
                    MAIN_MENU_BUTTONS
                )
        
                print(
                    "AI STEP 4 OK",
                    flush=True
                )
        
                # -------------------------------------------------
                # STAY IN AI QUESTION MODE
                # -------------------------------------------------
        
                set_session(
                    user_id,
                    "ASK_DRUG_QUESTION"
                )
        
                return jsonify({
                    "status": "ok"
                })
        
            except Exception as e:
        
                print(
                    "========================================",
                    flush=True
                )
        
                print(
                    "AI DRUG QUESTION ERROR",
                    flush=True
                )
        
                print(
                    "Error type:",
                    type(e).__name__,
                    flush=True
                )
        
                print(
                    "Error:",
                    repr(e),
                    flush=True
                )
        
                print(
                    "Question:",
                    repr(text),
                    flush=True
                )
        
                print(
                    "========================================",
                    flush=True
                )
        
                send_message(
                    chat_id,
                    "❌ هنگام پردازش سؤال مشکلی رخ داد.\n\n"
                    "لطفاً دوباره سؤال خود را ارسال کنید.",
                    MAIN_MENU_BUTTONS
                )
        
                set_session(
                    user_id,
                    "ASK_DRUG_QUESTION"
                )
        
                return jsonify({
                    "status": "ok"
                })
        

        
        # =================================================
        # MEDICATION LIST STATE
        # =================================================

        if state == "MEDICATION_LIST":

            medication = (
                get_medication_by_button(
                    user_id,
                    text
                )
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

        # =================================================
        # MEDICATION MANAGEMENT
        # =================================================

        if state == "MEDICATION_MANAGEMENT":

            medication_id = (
                session_data.get(
                    "medication_id"
                )
            )

            if not medication_id:

                show_medications(

                    chat_id,

                    user_id

                )

                return jsonify({
                    "status": "ok"
                })

            # -------------------------------------------------
            # Back to medication list
            # -------------------------------------------------

            if text == "↩️ داروهای من":

                show_medications(

                    chat_id,

                    user_id

                )

                return jsonify({
                    "status": "ok"
                })

            # -------------------------------------------------
            # Delete
            # -------------------------------------------------

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

                    "آیا از حذف این دارو مطمئن هستید؟",

                    [

                        ["✅ بله، حذف شود"],

                        ["❌ لغو"]

                    ]

                )

                return jsonify({
                    "status": "ok"
                })

            # -------------------------------------------------
            # Edit times
            # -------------------------------------------------

            if text == "✏️ تغییر زمان مصرف":

                set_session(

                    user_id,

                    "EDIT_TIMES",

                    {
                        "medication_id":
                            medication_id
                    }

                )

                send_message(

                    chat_id,

                    "⏰ زمان‌های مصرف جدید را وارد کنید.\n\n"
                    "مثلاً:\n"
                    "08:00, 20:00",

                    MAIN_MENU_BUTTONS

                )

                return jsonify({
                    "status": "ok"
                })

        # =================================================
        # CONFIRM DELETE
        # =================================================

        if state == "CONFIRM_DELETE":

            medication_id = (
                session_data.get(
                    "medication_id"
                )
            )

            if text == "✅ بله، حذف شود":

                with get_db_connection() as conn:

                    with conn.cursor() as cur:

                        # -------------------------------------------------
                        # ONLY NEW MAHROO MEDICATION TABLE
                        #
                        # Cascades automatically to:
                        # - mahroo_medication_schedules
                        # - mahroo_reminder_occurrences
                        # -------------------------------------------------

                        cur.execute("""
                            DELETE FROM mahroo_medications
                            WHERE id = %s
                              AND user_id = %s
                        """, (
                            medication_id,
                            user_id
                        ))

                        conn.commit()

                send_message(

                    chat_id,

                    "🗑 دارو حذف شد.",

                    MAIN_MENU_BUTTONS

                )

                set_session(

                    user_id,

                    "MAIN_MENU"

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

                    "💊 لطفاً نام دارو را وارد کنید.",

                    MAIN_MENU_BUTTONS

                )

                return jsonify({
                    "status": "ok"
                })

            set_session(

                user_id,

                "ASK_TIMES_PER_DAY",

                {
                    "medication_name":
                        text
                }

            )

            send_message(

                chat_id,

                "🔢 دارو چند بار در روز "
                "مصرف می‌شود؟\n\n"
                "مثلاً: 2",

                MAIN_MENU_BUTTONS

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # ASK TIMES PER DAY
        # =================================================

        if state == "ASK_TIMES_PER_DAY":

            try:

                doses_per_day = int(
                    text
                )

                if not (

                    1
                    <= doses_per_day
                    <= 20

                ):

                    raise ValueError

            except Exception:

                send_message(

                    chat_id,

                    "لطفاً یک عدد معتبر وارد کنید.\n"
                    "مثلاً: 2",

                    MAIN_MENU_BUTTONS

                )

                return jsonify({
                    "status": "ok"
                })

            session_data[
                "doses_per_day"
            ] = doses_per_day

            set_session(

                user_id,

                "ASK_NUMBER_OF_DOSES",

                session_data

            )

            send_message(

                chat_id,

                "💊 تعداد کل دوز موردنظر "
                "را وارد کنید.",

                MAIN_MENU_BUTTONS

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # ASK NUMBER OF DOSES
        # =================================================

        if state == "ASK_NUMBER_OF_DOSES":

            try:

                number_of_doses = int(
                    text
                )

                if number_of_doses < 1:

                    raise ValueError

            except Exception:

                send_message(

                    chat_id,

                    "لطفاً یک عدد معتبر وارد کنید.",

                    MAIN_MENU_BUTTONS

                )

                return jsonify({
                    "status": "ok"
                })

            session_data[
                "number_of_doses"
            ] = number_of_doses

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

                "⏰ زمان مصرف اول را وارد کنید.\n\n"
                "مثلاً: 08:00",

                MAIN_MENU_BUTTONS

            )

            return jsonify({
                "status": "ok"
            })

        
       
        # =================================================
        # ASK TIME
        # =================================================

        if state == "ASK_TIME":

            if not is_valid_time(
                text
            ):

                send_message(

                    chat_id,

                    "⏰ زمان واردشده معتبر نیست.\n"
                    "لطفاً به شکل HH:MM وارد کنید.\n\n"
                    "مثلاً: 08:00",

                    MAIN_MENU_BUTTONS

                )

                return jsonify({
                    "status": "ok"
                })

            session_data[
                "times"
            ].append(text)

            current_count = len(
                session_data["times"]
            )

            required_count = (
                session_data[
                    "doses_per_day"
                ]
            )

            if current_count < required_count:

                set_session(

                    user_id,

                    "ASK_TIME",

                    session_data

                )

                send_message(

                    chat_id,

                    f"⏰ زمان مصرف "
                    f"{current_count + 1} "
                    "را وارد کنید:",

                    MAIN_MENU_BUTTONS

                )

                return jsonify({
                    "status": "ok"
                })

            # -------------------------------------------------
            # All times collected
            # -------------------------------------------------

            session_data[
                "start_date"
            ] = None

            session_data[
                "end_date"
            ] = None

            set_session(

                user_id,

                "ASK_START_DATE",

                session_data

            )

            send_message(

                chat_id,

                "📅 تاریخ شروع مصرف دارو را وارد کنید.\n\n"
                "لطفاً تاریخ را به صورت زیر وارد کنید:\n"
                "YYYY/MM/DD\n\n"
                "مثلاً: 1405/06/20",

                MAIN_MENU_BUTTONS

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

                    name=session_data[
                        "medication_name"
                    ],

                    doses_per_day=session_data[
                        "doses_per_day"
                    ],

                    number_of_doses=session_data[
                        "number_of_doses"
                    ],

                    times=session_data[
                        "times"
                    ],

                    start_date=session_data[
                        "start_date"
                    ],

                    end_date=session_data[
                        "end_date"
                    ]

                )

                send_message(

                    chat_id,

                    "✅ دارو با موفقیت ثبت شد.\n\n"
                    "از این به بعد یادآوری‌های دارو فقط "
                    "در بازه تاریخ شروع تا تاریخ پایان "
                    "ارسال می‌شوند.",

                    MAIN_MENU_BUTTONS

                )

                set_session(

                    user_id,

                    "MAIN_MENU"

                )

                return jsonify({
                    "status": "ok"
                })



        # =================================================
        # EDIT TIMES
        # =================================================

        if state == "EDIT_TIMES":

            medication_id = (
                session_data.get(
                    "medication_id"
                )
            )

            raw_times = [

                x.strip()

                for x in text.split(",")

                if x.strip()

            ]

            if not raw_times:

                send_message(

                    chat_id,

                    "لطفاً زمان‌ها را وارد کنید.\n"
                    "مثلاً:\n"
                    "08:00, 20:00",

                    MAIN_MENU_BUTTONS

                )

                return jsonify({
                    "status": "ok"
                })

            for time in raw_times:

                if not is_valid_time(
                    time
                ):

                    send_message(

                        chat_id,

                        f"زمان «{time}» معتبر نیست.\n"
                        "فرمت صحیح: HH:MM",

                        MAIN_MENU_BUTTONS

                    )

                    return jsonify({
                        "status": "ok"
                    })

            with get_db_connection() as conn:

                with conn.cursor() as cur:

                    # -------------------------------------------------
                    # Deactivate old schedules
                    # -------------------------------------------------

                    cur.execute("""
                        UPDATE mahroo_medication_schedules
                        SET active = FALSE
                        WHERE medication_id = %s
                    """, (
                        medication_id,
                    ))

                    # -------------------------------------------------
                    # Insert new schedules
                    # -------------------------------------------------

                    for time in raw_times:

                        cur.execute("""
                            INSERT INTO mahroo_medication_schedules (
                                medication_id,
                                scheduled_time,
                                active
                            )
                            VALUES (
                                %s,
                                %s,
                                TRUE
                            )
                        """, (
                            medication_id,
                            time
                        ))

                    # -------------------------------------------------
                    # Update medication
                    # -------------------------------------------------

                    cur.execute("""
                        UPDATE mahroo_medications
                        SET doses_per_day = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                          AND user_id = %s
                    """, (
                        len(raw_times),
                        medication_id,
                        user_id
                    ))

                    conn.commit()

            send_message(

                chat_id,

                "✅ زمان‌های مصرف "
                "با موفقیت تغییر کرد.",

                MAIN_MENU_BUTTONS

            )

            show_medication_management(

                chat_id,

                user_id,

                medication_id

            )

            return jsonify({
                "status": "ok"
            })

        # =================================================
        # MEDICATION BUTTON FALLBACK
        # =================================================

        medication = (
            get_medication_by_button(
                user_id,
                text
            )
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

        # =================================================
        # FALLBACK
        # =================================================

        send_message(

            chat_id,

            "متوجه درخواست شما نشدم.\n\n"
            "می‌توانید از منوی پایین انتخاب کنید.",

            MAIN_MENU_BUTTONS

        )

        set_session(

            user_id,

            "MAIN_MENU"

        )

        return jsonify({
            "status": "ok"
        })

    except Exception as e:

        print(
            "receive_message ERROR:",
            repr(e)
        )

        return jsonify({
            "status": "ok"
        })


# =========================================================
# STARTUP
# =========================================================

try:

    init_database()

    print(
        "Mahroo database initialized successfully."
    )

except Exception as e:

    print(
        "Database initialization error:",
        repr(e)
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    port = int(

        os.getenv(
            "PORT",
            "5000"
        )

    )

    app.run(

        host="0.0.0.0",

        port=port

    )
