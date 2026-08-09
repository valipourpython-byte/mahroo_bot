```python
from flask import Flask, request, jsonify
import requests
import os
import psycopg

app = Flask(__name__)

# =========================
# Environment Variables
# =========================

TOKEN = os.getenv("BALE_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

BALE_API = f"https://tapi.bale.ai/bot{TOKEN}/sendMessage"


# =========================
# Database
# =========================

def get_db_connection():
    return psycopg.connect(DATABASE_URL)


def init_database():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL is not set")
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:

                # Users
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        bale_user_id BIGINT UNIQUE NOT NULL,
                        first_name TEXT,
                        username TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # Medications
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

                # Medication schedules
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

            conn.commit()

        print("Database initialized successfully.")

    except Exception as e:
        print("Database initialization error:", e)


# =========================
# Home
# =========================

@app.route("/")
def home():
    return "Mahroo backend is running 🚀"


# =========================
# Bale Webhook
# =========================

@app.route("/message", methods=["POST"])
def receive_message():

    data = request.json
    print("Received:", data)

    try:
        message = data.get("message", {})

        chat = message.get("chat", {})
        user = message.get("from", {})

        chat_id = chat.get("id")
        text = message.get("text", "")

        # -------------------------
        # Save user
        # -------------------------

        if user.get("id"):

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

                        RETURNING id;
                    """, (
                        user.get("id"),
                        user.get("first_name"),
                        user.get("username")
                    ))

                    db_user_id = cur.fetchone()[0]

                conn.commit()

            print("User saved:", db_user_id)

        # -------------------------
        # Current temporary reply
        # -------------------------

        reply_text = f"شما گفتید: {text}"

        response = requests.post(
            BALE_API,
            json={
                "chat_id": chat_id,
                "text": reply_text
            },
            timeout=15
        )

        print(
            "Bale response:",
            response.status_code,
            response.text
        )

    except Exception as e:
        print("Error:", e)

    return jsonify({"status": "ok"})


# =========================
# Start
# =========================

if __name__ == "__main__":

    init_database()

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )
```
