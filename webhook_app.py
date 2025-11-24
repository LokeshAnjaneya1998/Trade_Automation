# webhook_app.py

import logging
import asyncio
import os
from pathlib import Path
from flask import Flask, request, jsonify, abort, render_template_string
from fyers_integration import FyersIntegration
from waitress import serve

app = Flask(__name__)
fyers_integration = FyersIntegration()

@app.route("/", methods=["GET"])
def index():
    return "Fyers webhook server is running", 200

DEFAULT_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
LOG_LEVEL = os.getenv("WEBHOOK_LOG_LEVEL", "INFO").upper()



LOG_LEVEL = "INFO"  # or whatever you use

def configure_logging():
    base_dir = Path(__file__).resolve().parent
    log_dir = base_dir / "configandlogs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "webhook.log"

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),          # goes to journalctl
            logging.FileHandler(log_file)     # goes to configandlogs/webhook.log
        ]
    )

LOG_FILE = Path(__file__).resolve().parent / "configandlogs" / "webhook.log"

def read_log_tail(max_lines: int = 200) -> str:
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        tail = "".join(lines[-max_lines:])
    except FileNotFoundError:
        tail = "Log file not found. Trigger some activity first."
    return tail

def get_latest_auth_url(log_text: str) -> str | None:
    for line in reversed(log_text.splitlines()):
        if "Authorization URL:" in line:
            # Expecting: "... Authorization URL: https://...."
            return line.split("Authorization URL:")[-1].strip()
    return None

@app.route("/dashboard", methods=["GET"])
def dashboard():
    log_text = read_log_tail(200)
    auth_url = get_latest_auth_url(log_text)

    html = """
    <!doctype html>
    <html>
    <head>
        <title>Fyers Bot Dashboard</title>
        <style>
            body { font-family: sans-serif; margin: 20px; }
            .auth-box { margin-bottom: 20px; padding: 10px; border: 1px solid #ccc; }
            pre { background: #111; color: #eee; padding: 10px; overflow-x: auto; }
        </style>
    </head>
    <body>
        <h1>Fyers Bot Dashboard</h1>

        <div class="auth-box">
            <h2>Authentication Link</h2>
            {% if auth_url %}
                <p><a href="{{ auth_url }}" target="_blank">🔗 Click here to login to Fyers</a></p>
                <p><small>Copy-paste this URL if link click doesn’t work:<br>{{ auth_url }}</small></p>
            {% else %}
                <p>No Authorization URL found in recent logs.</p>
            {% endif %}
        </div>

        <h2>Recent Logs (last 200 lines)</h2>
        <pre>{{ log_text }}</pre>
    </body>
    </html>
    """

    return render_template_string(html, auth_url=auth_url, log_text=log_text)

@app.route("/capture_auth_code", methods=["GET"])
def capture_auth_code():
    auth_code = request.args.get("auth_code")
    if not auth_code:
        return "Authorization code not found.", 400

    fyers_integration.fetch_access_token(auth_code)
    return "Access token generated. You can close this tab."

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    

    if not data or "event" not in data:
        return jsonify({"--------------status": "Invalid webhook data"}), 400

    if data["event"] == "place_order":
        order_details = data.get("order_details", {})
        if order_details["side"] == "buy":
            order_details["side"] = 1
        if order_details["side"] == "sell":
            order_details["side"] = -1
        if order_details["symbol"] == "NSE:NIFTYBANK-INDEX":
            order_details["side"] = 1


        logging.info(f"--------[------order details: {order_details}")
        asyncio.run(place_order(order_details))
        return jsonify({"--------------status": "Order processing initiated."})

    return jsonify({"--------------status": "No valid event found"}), 400

async def place_order(order_details):
    try:
        fyers = fyers_integration.get_fyers_instance()
        response = fyers.place_order(order_details)
        logging.info(f"--------------Order response: {response}")
    except Exception as e:
        logging.error(f"--------------Order placement error: {e}")



def run_app():
    # Development-oriented server (Flask built-in)
    configure_logging()
    fyers_integration.generate_auth_url()
    app.run(host=DEFAULT_HOST, port=DEFAULT_PORT)


def run_production():
    # Production-ready server via Waitress (cross-platform WSGI)
    configure_logging()
    fyers_integration.generate_auth_url()
    serve(app, host=DEFAULT_HOST, port=DEFAULT_PORT)


if __name__ == "__main__":
    run_app()
