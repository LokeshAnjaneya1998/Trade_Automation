import logging
import asyncio
import os
import time
import threading
import re
from pathlib import Path
from functools import wraps
from logging.handlers import RotatingFileHandler

from flask import (
    Flask,
    request,
    jsonify,
    abort,
    render_template,
    session,
    redirect,
    url_for,
)
from src.fyers_integration import FyersIntegration
from waitress import serve

# ──────────────────────────────────────────────────────────────
# Global trading state
# ──────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

TRADING_ENABLED = True  # in-memory switch


def set_trading_enabled(value: bool):
    global TRADING_ENABLED
    TRADING_ENABLED = value
    logging.info(f"TRADING_ENABLED set to {TRADING_ENABLED}")


# ──────────────────────────────────────────────────────────────
# Flask app + Fyers integration
# ──────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parents[1]  # project root
TEMPLATES_DIR = BASE_DIR / "templates"

app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
fyers_integration = FyersIntegration()

LOG_FILE = BASE_DIR / "configandlogs" / "webhook.log"

# Admin credentials + secret key from environment (override defaults in systemd)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "LokeshTrading")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "LoIn2047@912")
SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.secret_key = SECRET_KEY  # for session cookies

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

DEFAULT_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
LOG_LEVEL = os.getenv("WEBHOOK_LOG_LEVEL", "INFO").upper()





# ──────────────────────────────────────────────────────────────
# Auth helpers
# ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            next_url = request.path
            return redirect(url_for("login", next=next_url))
        return f(*args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────────────────────
# Logging helpers
# ──────────────────────────────────────────────────────────────

def configure_logging():
    log_dir = BASE_DIR / "configandlogs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "webhook.log"

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB per file
        backupCount=5              # keep 5 old files
    )

    stream_handler = logging.StreamHandler()

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[stream_handler, file_handler],
    )


def read_log_tail(max_lines: int = 200) -> str:
    """Return last max_lines of the log, newest first."""
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        tail = lines[-max_lines:]
        tail.reverse()  # newest first
        return "".join(tail)
    except FileNotFoundError:
        return "Log file not found. Trigger some activity first."


def make_links(text: str) -> str:
    """Convert URLs in plain text into clickable HTML links."""
    url_pattern = r"(https?://[^\s]+)"
    return re.sub(url_pattern, r'<a href="\1" target="_blank">\1</a>', text)


def get_latest_auth_url(log_text: str) -> str | None:
    for line in reversed(log_text.splitlines()):
        if "Authorization URL:" in line:
            return line.split("Authorization URL:")[-1].strip()
    return None


# ──────────────────────────────────────────────────────────────
# Routes: Auth + Index
# ──────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        else:
            error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
def index():
    return "Fyers webhook server is running", 200


# ──────────────────────────────────────────────────────────────
# Routes: Dashboard + Logs
# ──────────────────────────────────────────────────────────────

@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    log_text = read_log_tail(200)
    auth_url = get_latest_auth_url(log_text)
    log_html = make_links(log_text)

    status_text = "ENABLED" if TRADING_ENABLED else "PAUSED"
    status_color = "green" if TRADING_ENABLED else "red"

    return render_template(
        "dashboard.html",
        auth_url=auth_url,
        log_html=log_html,
        admin_token=ADMIN_TOKEN,
        status_text=status_text,
        status_color=status_color,
    )


@app.route("/logs")
@login_required
def logs_only():
    log_text = read_log_tail(200)
    log_html = make_links(log_text)
    return log_html, 200


# ──────────────────────────────────────────────────────────────
# Routes: Fyers auth callback + webhook
# ──────────────────────────────────────────────────────────────

@app.route("/capture_auth_code", methods=["GET"])
def capture_auth_code():
    auth_code = request.args.get("auth_code")
    if not auth_code:
        return "Authorization code not found.", 400

    fyers_integration.fetch_access_token(auth_code)

    return """
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8" />
        <title>Fyers Auth Complete</title>
    </head>
    <body style="font-family: system-ui, sans-serif; text-align:center; padding-top:40px; background:#0b1120; color:#e5e7eb;">
        <h2>Access token generated.</h2>
        <p>You can close this tab.</p>
        <p>This window will close automatically in a few seconds...</p>
        <script>
            setTimeout(function() {
                window.close();
            }, 3000);
        </script>
    </body>
    </html>
    """



@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    logging.info(f"Incoming webhook: {data}")

    if not TRADING_ENABLED:
        logging.info(
            "Webhook received but TRADING_ENABLED is False. Ignoring order."
        )
        return jsonify({"status": "ignored", "reason": "trading_paused"}), 200

    if not data or "event" not in data:
        return jsonify({"status": "Invalid webhook data"}), 400

    if data["event"] == "place_order":
        order_details = data.get("order_details", {})

        # Basic safety
        side = order_details.get("side")
        symbol = order_details.get("symbol")
        if not side or not symbol:
            return jsonify({"status": "invalid_order_details"}), 400

        if side == "buy":
            order_details["side"] = 1
        elif side == "sell":
            order_details["side"] = -1

        if symbol == "NSE:NIFTYBANK-INDEX":
            order_details["side"] = 1

        logging.info(f"Order details: {order_details}")
        asyncio.run(place_order(order_details))
        return jsonify({"status": "Order processing initiated."}), 200

    return jsonify({"status": "No valid event found"}), 400


async def place_order(order_details):
    try:
        logging.info(f"Placing order via Fyers: {order_details}")
        fyers = fyers_integration.get_fyers_instance()
        response = fyers.place_order(order_details)
        logging.info(f"Order response: {response}")
    except Exception as e:
        logging.error(f"Order placement error: {e}")


# ──────────────────────────────────────────────────────────────
# Routes: Restart + Pause/Resume trading
# ──────────────────────────────────────────────────────────────

@app.route("/restart", methods=["GET", "POST"])
@login_required
def restart_service():
    token = request.form.get("token") or request.args.get("token")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        abort(403)

    def delayed_exit():
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=delayed_exit, daemon=True).start()

    return render_template(
        "message.html",
        title="Restarting bot...",
        message="Bot is restarting... you will be redirected to the dashboard.",
        redirect_url=url_for("dashboard"),
        delay_ms=3000,
    )


@app.route("/pause", methods=["POST"])
@login_required
def pause_trading():
    token = request.form.get("token") or request.args.get("token")
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        abort(403)

    set_trading_enabled(False)
    return render_template(
        "message.html",
        title="Trading paused",
        message="Trading paused (no orders will be sent). Redirecting to dashboard...",
        redirect_url=url_for("dashboard"),
        delay_ms=3000,
    )


@app.route("/resume", methods=["POST"])
@login_required
def resume_trading():
    token = request.form.get("token") or request.args.get("token")
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        abort(403)

    set_trading_enabled(True)
    return render_template(
        "message.html",
        title="Trading resumed",
        message="Trading resumed. Redirecting to dashboard...",
        redirect_url=url_for("dashboard"),
        delay_ms=3000,
    )


# ──────────────────────────────────────────────────────────────
# Entry points
# ──────────────────────────────────────────────────────────────

def run_app():
    configure_logging()
    fyers_integration.generate_auth_url()
    app.run(host=DEFAULT_HOST, port=DEFAULT_PORT, debug=True)


def run_production():
    configure_logging()
    logging.getLogger("waitress").setLevel(logging.ERROR)
    fyers_integration.generate_auth_url()
    serve(app, host=DEFAULT_HOST, port=DEFAULT_PORT)


if __name__ == "__main__":
    run_app()
