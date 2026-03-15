import logging
import os
import re
import subprocess
import threading
import time
import html
from pathlib import Path
from functools import wraps
from logging.handlers import RotatingFileHandler


from flask import (
    Flask,
    request,
    jsonify,
    Response,
    abort,
    render_template,
    session,
    redirect,
    url_for,
)

from waitress import serve
from datetime import timedelta

from src.premarket import PremarketAnalyzer
from src.fyers_integration import FyersIntegration
from src.config_manager import ConfigManager
from src.order_service import (
    parse_simple_alert,
    build_order_details_from_signal,
    dispatch_order,
    compute_cached_oi_pressure,
    get_last_order,
    get_recent_orders,
    get_option_chain_cache_info,
    get_profile_snapshot,
    check_market_open,
)


# ──────────────────────────────────────────────────────────────
# Global trading state
# ──────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

TRADING_ENABLED = True           # in-memory switch
PREMARKET_FILTER_ENABLED = False # gate orders on premarket analysis when True
CURRENT_SESSION_TOKEN: str | None = None  # track single active session
NIFTY_LOT_SIZE = 65              # current NSE lot size for NIFTY options
TRADING_LOTS = 1                 # number of lots to trade (configurable from dashboard)


def set_trading_enabled(value: bool):
    global TRADING_ENABLED
    TRADING_ENABLED = value
    logger.info(f"TRADING_ENABLED set to {TRADING_ENABLED}")


def _premarket_allows_trade(direction: str) -> tuple[bool, str]:
    """
    Check premarket trend + OI conditions for the trade direction.
    Trend comes from the 120s premarket cache; OI uses the 20s option-chain cache
    so the gate reacts to live market shifts rather than a 2-minute-old snapshot.
    Fails open (allows trade) if no data available — stale cache never silently blocks.
    """
    try:
        cached = premarket_analyzer._cache.get("data")
        if not cached:
            return True, "no_premarket_data"
        nr = (cached.get("summary") or {}).get("nifty_regime", {})
        trend = nr.get("trend_regime", "")

        # Prefer fresh OI (20s TTL) over the 120s premarket snapshot
        live_oi = compute_cached_oi_pressure()
        if live_oi:
            pressure = live_oi.get("pressure", "")
        else:
            pressure = ((cached.get("summary") or {}).get("oi_pressure") or {}).get("pressure", "")

        is_call = direction == "LONG_CALL"
        trend_ok = ("Uptrend" in trend) if is_call else ("Downtrend" in trend)
        oi_ok = ("PE-heavy" in pressure) if is_call else ("CE-heavy" in pressure)
        if not trend_ok and not oi_ok:
            return False, f"premarket_blocked: trend={trend!r} oi={pressure!r}"
        return True, "passed"
    except Exception as exc:
        logger.warning("Premarket filter check error: %s", exc)
        return True, "check_error"


# ──────────────────────────────────────────────────────────────
# Flask app + Fyers integration
# ──────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parents[1]  # project root
TEMPLATES_DIR = BASE_DIR / "templates"



app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
fyers_integration = FyersIntegration()
premarket_analyzer = PremarketAnalyzer(symbol="NIFTY")
config_manager = ConfigManager()
config = config_manager.config
APP_SECRET = (
    os.getenv("FLASK_SECRET_KEY")
    or os.getenv("SECRET_KEY")
    or config.secret_key
)

app.secret_key = APP_SECRET



# Make cookies work on http during local/dev runs unless overridden
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

app.config.update(
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,    # JS cannot read cookies
    SESSION_COOKIE_SAMESITE="Lax",   # mitigates CSRF; "Strict" if you want maximum lock
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=10),
)

LOG_FILE = BASE_DIR / "configandlogs" / "webhook.log"

# Admin credentials + secret key from environment (override defaults in systemd)

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
        global CURRENT_SESSION_TOKEN
        if not session.get("logged_in"):
            next_url = request.path
            return redirect(url_for("login", next=next_url))

        now = time.time()
        last_seen = session.get("last_seen")
        token = session.get("session_token")
        if CURRENT_SESSION_TOKEN and token != CURRENT_SESSION_TOKEN:
            session.clear()
            return redirect(url_for("login"))

        # If we've been inactive for > 10 minutes, force logout
        if last_seen is not None and now - last_seen > 10 * 60:
            session.clear()
            return redirect(url_for("login"))

        # Update last_seen on each valid request
        session["last_seen"] = now

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
    # Quiet noisy Fyers logs (JSON decode / 429 spam)
    for noisy in ("FyersAPI", "FyersAPIRequest"):
        lg = logging.getLogger(noisy)
        lg.setLevel(logging.ERROR)
        lg.propagate = False


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
    # log_text is already newest-first; scan in order and return first match
    for line in log_text.splitlines():
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
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # ✅ Compare with values from config.json
        if (
            username == config.web_login.username
            and password == config.web_login.password
        ):
            session.clear()
            session["logged_in"] = True
            session.permanent = False  # expire when browser closes
            session["last_seen"] = time.time()
            # single session token
            import uuid
            token = uuid.uuid4().hex
            session["session_token"] = token
            global CURRENT_SESSION_TOKEN
            CURRENT_SESSION_TOKEN = token
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)

        else:
            error = "Invalid username or password"

    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    global CURRENT_SESSION_TOKEN
    token = session.get("session_token")
    if CURRENT_SESSION_TOKEN and token == CURRENT_SESSION_TOKEN:
        CURRENT_SESSION_TOKEN = None
    session.clear()
    return redirect(url_for("login"))

@app.route("/logout_silent", methods=["POST"])
def logout_silent():
    global CURRENT_SESSION_TOKEN
    token = session.get("session_token")
    if CURRENT_SESSION_TOKEN and token == CURRENT_SESSION_TOKEN:
        CURRENT_SESSION_TOKEN = None
    # No redirect, just clear the session and return 204
    session.clear()
    return "", 204


@app.route("/", methods=["GET"])
def index():
    return "Fyers webhook server is running", 200
    

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
        premarket_filter_enabled=PREMARKET_FILTER_ENABLED,
    )


@app.route("/logs")
@login_required
def logs_only():
    log_text = read_log_tail(200)
    lines_raw = [line.rstrip() for line in log_text.splitlines() if line.strip() != ""]
    def linkify(line: str) -> str:
        return re.sub(r"(https?://[^\s]+)", r'<a href="\1" target="_blank">\1</a>', html.escape(line))
    lines = [linkify(line) for line in lines_raw]
    return render_template("logs.html", lines=lines)


@app.route("/logs/snippet")
@login_required
def logs_snippet():
    log_text = read_log_tail(10)
    lines = [line.rstrip() for line in log_text.splitlines() if line.strip() != ""]
    def linkify(line: str) -> str:
        return re.sub(r"(https?://[^\s]+)", r'<a href="\1" target="_blank">\1</a>', html.escape(line))
    body_lines = [f'<div class="log-line">{linkify(line)}</div>' for line in lines] if lines else ['<div class="log-line">No logs yet.</div>']
    sep = '<div class="log-separator"></div>'
    html_body = sep.join(body_lines)
    return app.response_class(html_body, mimetype="text/html")


@app.route("/logs/stream")
@login_required
def logs_stream():
    """
    Server-sent events stream for live log updates.
    """
    def event_stream():
        try:
            with open(LOG_FILE, "r") as f:
                f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        payload = make_links(line.rstrip())
                        yield f"data: {payload}\n\n"
                    else:
                        time.sleep(0.5)
        except FileNotFoundError:
            yield "data: \n\n"
    return Response(event_stream(), mimetype="text/event-stream")


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
            }, 1000);
        </script>
    </body>
    </html>
    """



@app.route("/webhook", methods=["POST"])
def webhook():
    def _build_and_dispatch(mode: str, direction: str, setup_type: str, side: str, qty: int | None = None):
        try:
            order_details, note = build_order_details_from_signal(
                fyers_integration,
                direction=direction,
                setup_type=setup_type,
                side=side,
                qty=qty,
            )
        except Exception as exc:
            msg = str(exc)
            if "429" in msg or "throttle" in msg.lower():
                logger.warning("Order build failed due to throttling: %s", msg)
                return jsonify({"status": "error", "detail": "Fyers throttled (429) while fetching quotes; retry shortly."}), 503
            logger.error("Order build failed: %s", msg)
            return jsonify({"status": "error", "detail": msg}), 500
        logger.info(f"[{mode}] {note}")
        logger.info(f"[{mode}] Order details: {order_details}")
        dispatch_order(fyers_integration, order_details)
        return jsonify({"status": "order_processing_initiated", "mode": mode}), 200

    # Try JSON first (legacy Pine webhook)
    data = request.get_json(force=True, silent=True)
    raw_body = request.data.decode("utf-8", errors="ignore")
    qty_override = None
    if isinstance(data, dict):
        try:
            qty_override = data.get("qty") or data.get("quantity")
            if qty_override is not None:
                qty_override = int(qty_override)
        except Exception:
            qty_override = None
    if qty_override is None:
        qty_qp = request.args.get("qty") or request.args.get("quantity")
        if qty_qp:
            try:
                qty_override = int(qty_qp)
            except Exception:
                qty_override = None
    # Fall back to dashboard-configured lots when webhook doesn't specify qty
    if qty_override is None:
        qty_override = TRADING_LOTS * NIFTY_LOT_SIZE

    logger.info(f"Incoming webhook json: {data}")

    if not TRADING_ENABLED:
        logger.info("Webhook received but TRADING_ENABLED is False. Ignoring order.")
        return jsonify({"status": "ignored", "reason": "trading_paused"}), 200

    is_open, market_reason = check_market_open(fyers_integration)
    if not is_open:
        logger.info("Webhook received but market is closed (%s). Ignoring order.", market_reason)
        return jsonify({"status": "ignored", "reason": f"market_closed:{market_reason}"}), 200

    # Premarket filter gate — checked per signal direction after parsing
    def _apply_premarket_gate(direction: str):
        if not PREMARKET_FILTER_ENABLED:
            return None  # filter off, proceed
        allowed, reason = _premarket_allows_trade(direction)
        if not allowed:
            logger.info("Premarket filter blocked order: %s", reason)
            return jsonify({"status": "ignored", "reason": reason}), 200
        return None

    # ───────────── Legacy mode: Pine sends full JSON ─────────────
    if isinstance(data, dict) and data.get("event") == "place_order":
        order_details = data.get("order_details", {})

        side = order_details.get("side")
        symbol = order_details.get("symbol")
        opt_strike = str(order_details.get("optStrike", "") or "").strip()
        opt_type_raw = order_details.get("optType")
        opt_type = str(opt_type_raw or "").upper().strip()

        if not side or not symbol:
            return jsonify({"status": "invalid_order_details"}), 400
        if not opt_type:
            return jsonify({"status": "invalid_order_details", "reason": "missing_optType"}), 400
        if opt_type not in {"CE", "PE"}:
            return jsonify({"status": "invalid_order_details", "reason": "invalid_optType", "optType": opt_type_raw}), 400

        # If legacy payload is the index (no strike), build option selection first.
        needs_selection = (
            symbol.upper().endswith("NIFTY50-INDEX")
            or symbol.upper().endswith("NIFTY 50")
            or opt_strike == ""
        )

        if needs_selection:
            direction = "LONG_CALL" if opt_type == "CE" else "LONG_PUT"
            gate = _apply_premarket_gate(direction)
            if gate:
                return gate
            try:
                setup_type_inferred = "EXIT" if str(side) in {"-1", "sell", "-"} else "BREAKOUT"
                return _build_and_dispatch("LEGACY->PY", direction, setup_type_inferred, side, qty_override)
            except Exception as exc:
                logger.error(f"Legacy payload failed to build option: {exc}")
                return jsonify({"status": "error", "detail": str(exc)}), 500

    

        logging.info(f"[LEGACY] Order details: {order_details}")
        dispatch_order(fyers_integration, order_details)
        return jsonify({"status": "order_processing_initiated", "mode": "legacy"}), 200

    # ───────────── New mode: simple alert text ─────────────
    alert_text = raw_body.strip()
    side, direction, setup_type = parse_simple_alert(alert_text)

    if not side or not direction or not setup_type:
        return jsonify({"status": "ignored", "reason": "unrecognized_alert", "alert": alert_text}), 200

    gate = _apply_premarket_gate(direction)
    if gate:
        return gate

    try:
        return _build_and_dispatch("PY-SIGNAL", direction, setup_type, side, qty_override)
    except ValueError as exc:
        # Common case: Fyers access token missing/not generated yet
        logger.error(f"Webhook blocked: {exc}")
        return jsonify({
            "status": "error",
            "reason": "fyers_auth_required",
            "detail": str(exc),
        }), 428
    except Exception as exc:
        logger.error(f"Webhook failed to build order: {exc}")
        return jsonify({"status": "error", "detail": str(exc)}), 500

 



# ──────────────────────────────────────────────────────────────
# Routes: Restart + Pause/Resume trading
# ──────────────────────────────────────────────────────────────

@app.route("/restart", methods=["GET", "POST"])
@login_required
def restart_service():
    token = request.form.get("token") or request.args.get("token")
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        abort(403)

    # Run the exact command provided (or a safe default) in the background after a short delay,
    # so the HTTP response completes before the service restarts.
    default_cmd = f"sudo systemctl restart {os.getenv('BOT_SERVICE_NAME', 'fyersbot')}"
    restart_cmd = os.getenv("BOT_RESTART_CMD", default_cmd).strip()
    if not restart_cmd:
        return jsonify({
            "status": "error",
            "detail": "Restart command is empty; set BOT_RESTART_CMD or BOT_SERVICE_NAME.",
        }), 500

    try:
        wrapper_cmd = ["/bin/bash", "-lc", f"sleep 1; {restart_cmd}"]
        subprocess.Popen(
            wrapper_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("Restart scheduled via: %s", restart_cmd)
        message = f"Bot restart triggered. Command: {restart_cmd}"
    except Exception as exc:
        logger.error("Restart failed: %s", exc)
        return jsonify({
            "status": "error",
            "detail": f"Unexpected restart failure: {exc}",
            "command": restart_cmd,
        }), 500

    return render_template(
        "message.html",
        title="Restarting bot...",
        message=message + " You will be redirected to the dashboard.",
        redirect_url=url_for("dashboard"),
        delay_ms=1000,
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
        delay_ms=1000,
    )


@app.route("/toggle_premarket_filter", methods=["POST"])
@login_required
def toggle_premarket_filter():
    global PREMARKET_FILTER_ENABLED
    token = request.form.get("token") or request.args.get("token")
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        abort(403)
    PREMARKET_FILTER_ENABLED = not PREMARKET_FILTER_ENABLED
    state = "enabled" if PREMARKET_FILTER_ENABLED else "disabled"
    logger.info("Premarket filter %s", state)
    return render_template(
        "message.html",
        title=f"Premarket filter {state}",
        message=f"Premarket filter {state}. Redirecting to dashboard...",
        redirect_url=url_for("dashboard"),
        delay_ms=1000,
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
        delay_ms=1000,
    )


@app.route("/api/premarket/summary", methods=["GET"])
@login_required
def premarket_summary():
    """
    Returns premarket summary, checkpoints and suggestions as JSON
    so the dashboard can render them.
    """
    try:
        data = premarket_analyzer.analyze_as_dict()
    except ValueError as exc:
        # Common case: Fyers access token missing/not generated yet
        logger.error(f"Premarket analysis blocked: {exc}")
        return jsonify({
            "error": "fyers_auth_required",
            "detail": str(exc),
            "action": "Authenticate with Fyers to generate an access token, then retry."
        }), 428
    except Exception as exc:
        msg = str(exc)
        if "throttle" in msg.lower() or "429" in msg:
            logger.warning(f"Premarket analysis throttled: {exc}")
        else:
            logger.error(f"Premarket analysis failed: {exc}")
        return jsonify({"error": "Premarket analysis failed", "detail": msg}), 503

    return jsonify(data), 200


@app.route("/api/optionchain/summary", methods=["GET"])
@login_required
def optionchain_summary():
    """
    Lightweight option chain snapshot for the dashboard.
    """
    oc = compute_cached_oi_pressure()
    if not oc:
        return jsonify({"error": "No option chain data"}), 503
    return jsonify(oc), 200


@app.route("/api/last_order", methods=["GET"])
@login_required
def last_order():
    """
    Returns the most recent built order + note for dashboard display.
    """
    data = get_last_order()
    if not data:
        return jsonify({"error": "No orders yet"}), 404
    return jsonify(data), 200


@app.route("/api/recent_orders", methods=["GET"])
@login_required
def recent_orders():
    """
    Returns recent built orders (up to 5) for dashboard.
    """
    orders = get_recent_orders()
    return jsonify({"orders": orders}), 200


@app.route("/api/lots", methods=["GET"])
@login_required
def get_lots():
    return jsonify({"lots": TRADING_LOTS, "lot_size": NIFTY_LOT_SIZE, "qty": TRADING_LOTS * NIFTY_LOT_SIZE}), 200


@app.route("/api/lots", methods=["POST"])
@login_required
def set_lots():
    global TRADING_LOTS
    data = request.get_json(force=True, silent=True) or {}
    try:
        lots = int(data.get("lots", 1))
        if lots < 1:
            return jsonify({"status": "error", "detail": "Lots must be >= 1"}), 400
    except (TypeError, ValueError):
        return jsonify({"status": "error", "detail": "Invalid lots value"}), 400
    TRADING_LOTS = lots
    logger.info("TRADING_LOTS set to %d (qty=%d)", TRADING_LOTS, TRADING_LOTS * NIFTY_LOT_SIZE)
    return jsonify({"status": "ok", "lots": TRADING_LOTS, "lot_size": NIFTY_LOT_SIZE, "qty": TRADING_LOTS * NIFTY_LOT_SIZE}), 200


@app.route("/api/health", methods=["GET"])
@login_required
def health():
    """
    Surface basic health: trading toggle, fyers token presence, option chain cache age.
    """
    oc_info = get_option_chain_cache_info()
    token_present = bool(config.access_token)
    try:
        profile = get_profile_snapshot(fyers_integration)
    except Exception as exc:
        profile = {"error": str(exc)}
    return jsonify({
        "trading_enabled": TRADING_ENABLED,
        "fyers_auth": "present" if token_present else "missing",
        "option_chain": oc_info,
        "profile": profile,
    }), 200


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
