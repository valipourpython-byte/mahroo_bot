from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

TOKEN = "YOUR_BALE_BOT_TOKEN"
BALE_API = f"https://tapi.bale.ai/bot{TOKEN}/sendMessage"

@app.route("/")
def home():
    return "Mahroo backend is running 🚀"

@app.route("/message", methods=["POST"])
def receive_message():
    data = request.json
    print("Received:", data)

    try:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        reply_text = f"شما گفتید: {text}"

        response = requests.post(BALE_API, json={
            "chat_id": chat_id,
            "text": reply_text
        })

        print("Bale response:", response.status_code, response.text)

    except Exception as e:
        print("Error:", e)

    return jsonify({"status": "ok"})

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
