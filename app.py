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

        requests.post(BALE_API, json={
            "chat_id": chat_id,
            "text": reply_text
        })

    except Exception as e:
        print("Error:", e)

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)