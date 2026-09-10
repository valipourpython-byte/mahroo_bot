from flask import Flask, request, jsonify
import requests
import os
import psycopg
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


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
            "ERROR: OPENROUTER_API_KEY is not configured."
        )

        return None

    system_prompt = """
تو دستیار هوشمند دارویی «مهرو» هستی.

وظیفه تو پاسخ‌گویی فارسی به پرسش‌های کاربران درباره داروها
و اطلاعات دارویی است.

قوانین مهم:

1. فقط در حوزه اطلاعات دارویی پاسخ بده.

2. اگر سؤال کاربر ارتباطی با دارو، مصرف دارو، عوارض،
موارد مصرف، تداخلات، هشدارها، منع مصرف، شکل دارویی
یا اطلاعات مرتبط با دارو ندارد، پاسخ بده:

«این سؤال در حوزه اطلاعات دارویی مهرو نیست.»

3. اطلاعاتی که در CONTEXT ارائه شده را منبع اصلی اطلاعات
دارویی در نظر بگیر.

4. اطلاعاتی را که در CONTEXT وجود ندارد، به عنوان واقعیت
قطعی درباره داروی موردنظر اختراع نکن.

5. اگر اطلاعات کافی در CONTEXT وجود ندارد، صریحاً بگو
که اطلاعات کافی در پایگاه داده دارویی مهرو موجود نیست.

6. پاسخ را به زبان فارسی و برای یک کاربر عادی بنویس.

7. نام انگلیسی دارو را در صورت مفید بودن داخل پرانتز بیاور.

8. پاسخ کوتاه، واضح و قابل فهم باشد.

9. از ارائه تشخیص پزشکی قطعی خودداری کن.

10. در مسائل پزشکی حساس، از دادن دستور قطعی و
شخصی‌سازی‌شده برای تغییر، قطع یا شروع دارو خودداری کن
و کاربر را به پزشک یا داروساز ارجاع بده.

11. اگر کاربر درباره دوز شخصی، تغییر دوز، قطع دارو،
شروع دارو یا جایگزین کردن دارو سؤال کرد، با احتیاط پاسخ بده
و توصیه به مشورت با پزشک یا داروساز کن.

12. اگر چند دارو در سؤال مطرح شده‌اند، تا حد امکان
اطلاعات مربوط به هر دارو را جداگانه بیان کن.

13. اطلاعات CONTEXT را خلاصه، منظم و قابل فهم ارائه کن.

14. از ساختن اطلاعاتی که در CONTEXT وجود ندارد خودداری کن.
"""

    user_prompt = f"""
سؤال کاربر:

{user_question}

--------------------------------

اطلاعات بازیابی‌شده از پایگاه داده دارویی مهرو:

{
    drug_context
    if drug_context
    else
    "اطلاعات دارویی مشخصی برای این سؤال پیدا نشد."
}
"""

    payload = {

        "model":
            OPENROUTER_MODEL,

        "messages": [

            {
                "role":
                    "system",

                "content":
                    system_prompt
            },

            {
                "role":
                    "user",

                "content":
                    user_prompt
            }
        ],

        "temperature":
            0.2,

        "max_tokens":
            500
    }

    headers = {

        "Authorization":
            f"Bearer {OPENROUTER_API_KEY}",

        "Content-Type":
            "application/json",

        "HTTP-Referer":
            "https://htvsai.app",

        "X-Title":
            "Mahroo"
    }
    try:

        print("========== ASK_LLM CALLED ==========")
        print("Question:", repr(user_question))
        print("Model:", OPENROUTER_MODEL)
        print("API KEY EXISTS:", bool(OPENROUTER_API_KEY))
        print("Context length:", len(drug_context or ""))
        print("====================================")
    
        response = requests.post(
    
            "https://openrouter.ai/api/v1/chat/completions",
    
            headers=headers,
    
            json=payload,
    
            timeout=60
        )
    
        print(
            "========== OPENROUTER RESPONSE =========="
        )
        print("Status code:", response.status_code)
        print("Response:", response.text[:3000])
        print(
            "=========================================="
        )
    
        if not response.ok:
    
            print(
                "OpenRouter error:",
                response.status_code,
                response.text
            )
    
            return None
    
        data = response.json()
    
        choices = data.get(
            "choices",
            []
        )
    
        if not choices:
    
            print(
                "OpenRouter returned no choices"
            )
    
            return None
    
        answer = (
            choices[0]
            .get("message", {})
            .get("content")
        )
    
        if not answer:
    
            print(
                "OpenRouter returned empty answer"
            )
    
            return None
    
        return answer.strip()
    
    except Exception as e:
    
        print(
            "OpenRouter exception:",
            repr(e)
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
# FIND DRUGS IN USER QUESTION
# =========================================================

def find_drugs_in_question(
    question
):

    question = clean_drug_text(
        question
    )

    if not question:

        return []

    found_drugs = []

    with get_db_connection() as conn:

        with conn.cursor() as cur:

            cur.execute("""
                SELECT
                    generic_rxcui,
                    generic_tty,
                    generic_name
                FROM drugs
                WHERE LOWER(%s)
                      LIKE '%' ||
                      LOWER(generic_name) ||
                      '%'
                ORDER BY
                    LENGTH(generic_name) DESC
                LIMIT 3;
            """, (
                question,
            ))

            rows = cur.fetchall()

            for row in rows:

                drug = search_drug_database(
                    row[2]
                )

                if drug:

                    found_drugs.append(
                        drug
                    )

    return found_drugs


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

    lines = []

    lines.append(
        "💊 اطلاعات دارو"
    )

    lines.append("")

    lines.append(

        f"نام دارو: "
        f"{drug.get('generic_name', '-')}"

    )

    lines.append(

        f"نوع: "
        f"{drug.get('generic_tty', '-')}"

    )

    lines.append(

        f"RxCUI: "
        f"{drug.get('generic_rxcui', '-')}"

    )

    # =====================================================
    # PRODUCTS
    # =====================================================

    products = drug.get(
        "products",
        []
    )

    if products:

        lines.append("")

        lines.append(
            "📦 فرآورده‌ها:"
        )

        for product in products[:10]:

            name = product.get(
                "product_name"
            )

            if name:

                lines.append(
                    f"• {name}"
                )

    # =====================================================
    # SECTIONS
    # =====================================================

    sections = [

        (
            "🩺 موارد مصرف",
            "indications"
        ),

        (
            "⚠️ عوارض جانبی",
            "side_effects"
        ),

        (
            "🚫 موارد منع مصرف",
            "contraindications"
        ),

        (
            "⚠️ هشدارها",
            "warnings"
        ),

        (
            "ℹ️ احتیاط‌ها",
            "precautions"
        ),

        (
            "🔄 تداخلات",
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

        lines.append("")

        lines.append(title)

        for value in values[:8]:

            lines.append(
                f"• {value}"
            )

    lines.append("")

    lines.append(

        "ℹ️ این اطلاعات از پایگاه داده "
        "دارویی مهرو استخراج شده است."

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
    times
):

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
                    active
                )
                VALUES (
                    %s,
                    %s,
                    %s,
                    %s,
                    TRUE
                )
                RETURNING id
            """, (
                user_id,
                name,
                doses_per_day,
                number_of_doses
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
            """)

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


# =========================================================
# RECEIVE MESSAGE
# =========================================================

@app.route(
    "/message",
    methods=["POST"]
)
def receive_message():

    try:

        # =================================================
        # READ BALE UPDATE
        # =================================================

        data = request.get_json(
            silent=True
        ) or {}

        print(

            "Incoming Bale message:",

            json.dumps(

                data,

                ensure_ascii=False

            )

        )

        # -------------------------------------------------
        # Bale payload
        # -------------------------------------------------

        message = (

            data.get("message")

            or data.get("result")

            or data

        )

        if not isinstance(
            message,
            dict
        ):

            return jsonify({
                "status": "ok"
            })

        user = (

            message.get("from")

            or message.get("user")

            or {}

        )

        chat = (

            message.get("chat")

            or {}

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

        text = clean_drug_text(
            text
        )

        print(
            "Incoming text:",
            repr(text)
        )

        # =================================================
        # CHAT ID
        # =================================================

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

        if chat_id is None:

            print(
                "ERROR: chat_id not found"
            )

            return jsonify({
                "status": "ok"
            })

        chat_id = str(
            chat_id
        )

        # =================================================
        # USER
        # =================================================

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

        state, session_data = (

            get_session(
                user_id
            )

        )
        print("========== MESSAGE DEBUG ==========")
        print("USER ID:", repr(user_id))
        print("TEXT:", repr(text))
        print("STATE:", repr(state))
        print("SESSION DATA:", repr(session_data))
        print("===================================")
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
        # AI DRUG QUESTION STATE
        # =================================================

        

        if state == "ASK_DRUG_QUESTION":
        
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
                "========================================"
            )
            print(
                "AI QUESTION RECEIVED"
            )
            print(
                "Question:",
                repr(text)
            )
            print(
                "User ID:",
                user_id
            )
            print(
                "========================================"
            )
        
            try:
        
                # -------------------------------------------------
                # STEP 1: FIND DRUGS
                # -------------------------------------------------
        
                print(
                    "AI STEP 1: Finding drugs..."
                )
        
                drugs = find_drugs_in_question(
                    text
                )
        
                print(
                    "AI STEP 1 OK"
                )
        
                print(
                    "Detected drugs:",
                    [
                        d.get("generic_name")
                        for d in drugs
                    ]
                )
        
                # -------------------------------------------------
                # STEP 2: BUILD CONTEXT
                # -------------------------------------------------
        
                print(
                    "AI STEP 2: Building drug context..."
                )
        
                contexts = []
        
                for drug in drugs[:3]:
        
                    print(
                        "Building context for:",
                        drug.get("generic_name")
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
                    "AI STEP 2 OK"
                )
        
                print(
                    "Drug context length:",
                    len(drug_context)
                )
        
                # -------------------------------------------------
                # STEP 3: CALL LLM
                # -------------------------------------------------
        
                print(
                    "AI STEP 3: Calling OpenRouter..."
                )
        
                answer = ask_llm(
                    user_question=text,
                    drug_context=drug_context
                )
        
                print(
                    "AI STEP 3 COMPLETED"
                )
        
                # -------------------------------------------------
                # LLM ERROR
                # -------------------------------------------------
        
                if not answer:
        
                    print(
                        "AI ERROR: ask_llm returned None/empty"
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
                    "AI STEP 4: Sending answer..."
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
                    "AI STEP 4 OK"
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
                    "========================================"
                )
        
                print(
                    "AI DRUG QUESTION ERROR"
                )
        
                print(
                    "Error type:",
                    type(e).__name__
                )
        
                print(
                    "Error:",
                    repr(e)
                )
        
                print(
                    "Question:",
                    repr(text)
                )
        
                print(
                    "========================================"
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
        # DRUG SEARCH STATE
        # =================================================

        if state == "ASK_DRUG_SEARCH":

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

                drug = (
                    search_drug_database(
                        text
                    )
                )

                if not drug:

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

                result_text = (
                    format_drug_result(
                        drug
                    )
                )

                send_message(

                    chat_id,

                    result_text,

                    MAIN_MENU_BUTTONS

                )

                # Stay in search mode
                set_session(

                    user_id,

                    "ASK_DRUG_SEARCH"

                )

                return jsonify({
                    "status": "ok"
                })

            except Exception as e:

                print(

                    "Drug search error:",

                    repr(e)

                )

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

            set_session(

                user_id,

                "CONFIRM_MEDICATION",

                session_data

            )

            times_text = "\n".join(

                f"• {time}"

                for time
                in session_data["times"]

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
                    ]

                )

                send_message(

                    chat_id,

                    "✅ دارو با موفقیت ثبت شد.",

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
