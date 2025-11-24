# webhook_app.py

import logging
import asyncio
import os
from pathlib import Path
from flask import Flask, request, jsonify, abort, render_template_string, session, redirect, url_for
from fyers_integration import FyersIntegration
from waitress import serve
import re
import time
import threading
from functools import wraps




TRADING_ENABLED = True  # global in-memory switch

def set_trading_enabled(value: bool):
    global TRADING_ENABLED
    TRADING_ENABLED = value
    logging.info(f"TRADING_ENABLED set to {TRADING_ENABLED}")

app = Flask(__name__)
fyers_integration = FyersIntegration()


ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "LokeshTrading")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "LoIn2047@912")

SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
app.secret_key = SECRET_KEY  # for session cookies


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            # optional: remember where user wanted to go
            next_url = request.path
            return redirect(url_for("login", next=next_url))
        return f(*args, **kwargs)
    return wrapper

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["logged_in"] = True
            # redirect to dashboard (or "next" if present)
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        else:
            error = "Invalid username or password"

    html = """
    <!doctype html>
    <html>
    <head>
        <title>Login – Fyers Bot Dashboard</title>
        <meta charset="utf-8" />
        <style>
            body { font-family: sans-serif; margin: 40px; }
            form { max-width: 300px; }
            label { display:block; margin-top:10px; }
            input { width:100%; padding:6px; margin-top:4px; }
            button { margin-top:15px; padding:8px 16px; }
            .error { color: red; }
        </style>
    </head>
    <body>
        <h1>Login</h1>
        {% if error %}
            <p class="error">{{ error }}</p>
        {% endif %}
        <form method="POST">
            <label>Username
                <input type="text" name="username" autofocus />
            </label>
            <label>Password
                <input type="password" name="password" />
            </label>
            <button type="submit">Login</button>
        </form>
    </body>
    </html>
    """
    return render_template_string(html, error=error)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
def index():
    return "Fyers webhook server is running", 200

DEFAULT_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("WEBHOOK_PORT", "5000"))
LOG_LEVEL = os.getenv("WEBHOOK_LOG_LEVEL", "INFO").upper()


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
        tail = lines[-max_lines:]
        tail.reverse()  # newest first
        return "".join(tail)
    except FileNotFoundError:
        return "Log file not found. Trigger some activity first."
    
def make_links(text):
        url_pattern = r'(https?://[^\s]+)'
        return re.sub(url_pattern, r'<a href="\1" target="_blank">\1</a>', text)

def get_latest_auth_url(log_text: str) -> str | None:
    for line in reversed(log_text.splitlines()):
        if "Authorization URL:" in line:
            # Expecting: "... Authorization URL: https://...."
            return line.split("Authorization URL:")[-1].strip()
    return None

@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    log_text = read_log_tail(200)

    # Extract latest auth URL
    auth_url = get_latest_auth_url(log_text)

    

    log_html = make_links(log_text)

    status_text = "ENABLED" if TRADING_ENABLED else "PAUSED"
    status_color = "green" if TRADING_ENABLED else "red"

    html = """
    <!doctype html>
    <html>
    <head>
        <title>Fyers Bot Dashboard</title>
        <style>
            body { font-family: sans-serif; margin: 20px; }
            .auth-box { margin-bottom: 20px; padding: 12px; border: 1px solid #ccc; background: #fafafa; }
            pre { background: #111; color: #eee; padding: 10px; overflow-x: auto; white-space: pre-wrap; }
            a { color: #4EA5F3; }
        </style>
    </head>
    <body>
        <h1>Fyers Bot Dashboard</h1>

        <form method="POST" action="/restart" style="margin-bottom: 20px;">
            <input type="hidden" name="token" value="{{ admin_token }}">
            <button type="submit">🔄 Restart Bot</button>
        </form>

        <p style="float:right;">
            <a href="{{ url_for('logout') }}">Logout</a>
        </p>

        <p>
            Trading status:
            <strong style="color: {{ status_color }};">{{ status_text }}</strong>
        </p>

        <form method="POST" action="/resume" style="display:inline-block;margin-right:10px;">
            <input type="hidden" name="token" value="{{ admin_token }}">
            <button type="submit" style="background-color:green;color:white;padding:8px 16px;border:none;">
                ▶ Start Trading
            </button>
        </form>

        <form method="POST" action="/pause" style="display:inline-block;margin-right:10px;">
            <input type="hidden" name="token" value="{{ admin_token }}">
            <button type="submit" style="background-color:red;color:white;padding:8px 16px;border:none;">
                ⏸ Stop Trading
            </button>
        </form>



        <div class="auth-box">
            <h2>Authentication Link</h2>
            {% if auth_url %}
                <p><a href="{{ auth_url }}" target="_blank">🔗 Click here to login to Fyers</a></p>
                <p><small>{{ auth_url }}</small></p>
            {% else %}
                <p>No Authorization URL found in recent logs.</p>
            {% endif %}
        </div>

        <h2>Recent Logs (last 200 lines)</h2>
        <pre id="log-box">{{ log_html|safe }}</pre>
        <script>
            function refreshLogs() {
                fetch('/logs')
                    .then(response => response.text())
                    .then(html => {
                        document.getElementById('log-box').innerHTML = html;
                    })
                    .catch(err => console.error("Log refresh failed:", err));
            }

            // Refresh logs every 5 seconds (only the logs box)
            setInterval(refreshLogs, 1000);
        </script>
    </body>
    </html>
    """

    return render_template_string(html, auth_url=auth_url, log_html=log_html, admin_token=ADMIN_TOKEN, status_text=status_text, status_color=status_color)

@app.route("/logs")
@login_required
def logs_only():
    log_text = read_log_tail(200)
    log_html = make_links(log_text)
    return log_html, 200

@app.route("/capture_auth_code", methods=["GET"])
def capture_auth_code():
    auth_code = request.args.get("auth_code")
    if not auth_code:
        return "Authorization code not found.", 400

    fyers_integration.fetch_access_token(auth_code)
    return "Access token generated. You can close this tab."

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True, silent=True) or {}
    logging.info(f"Incoming webhook: {data}")
    
    if not TRADING_ENABLED:
        logging.info("Webhook received but TRADING_ENABLED is False. Ignoring order.")
        return jsonify({"status": "ignored", "reason": "trading_paused"}), 200

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

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

@app.route("/restart", methods=["GET","POST"])
@login_required
def restart_service():
    token = request.form.get("token") or request.args.get("token")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        abort(403)

    # respond immediately, then exit in background so systemd restarts us
    def delayed_exit():
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=delayed_exit, daemon=True).start()
    return """
    <!doctype html>
    <html>
      <head>
        <title>Restarting bot...</title>
        <meta charset="utf-8" />
      </head>
      <body>
        <p>Bot is restarting... you will be redirected to the dashboard in a few seconds.</p>
        <script>
          setTimeout(function() {
            window.location.href = "/dashboard";
          }, 3000);  // 3 seconds
        </script>
      </body>
    </html>
    """, 200



@app.route("/pause", methods=["POST"])
@login_required
def pause_trading():
    token = request.form.get("token") or request.args.get("token")
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        abort(403)

    set_trading_enabled(False)
    return """
        <!doctype html>
        <html>
        <head>
            <title>Restarting bot...</title>
            <meta charset="utf-8" />
        </head>
        <body>
            <p>Trading paused (no orders will be sent).... you will be redirected to the dashboard in a few seconds.</p>
            <script>
            setTimeout(function() {
                window.location.href = "/dashboard";
            }, 3000);  // 3 seconds
            </script>
        </body>
        </html>
        """, 200

@app.route("/resume", methods=["POST"])
@login_required
def resume_trading():
    token = request.form.get("token") or request.args.get("token")
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        abort(403)

    set_trading_enabled(True)
    return """
            <!doctype html>
            <html>
            <head>
                <title>Restarting bot...</title>
                <meta charset="utf-8" />
            </head>
            <body>
                <p>Trading resumed.... you will be redirected to the dashboard in a few seconds.</p>
                <script>
                setTimeout(function() {
                    window.location.href = "/dashboard";
                }, 3000);  // 3 seconds
                </script>
            </body>
            </html>
            """, 200


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
