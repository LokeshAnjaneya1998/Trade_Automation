import logging
import os
import time
import pytz
import threading
import re
import html
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

from waitress import serve
from datetime import timedelta, datetime

from src.premarket import PremarketAnalyzer
from src.option_selection import OptionSelection, select_nifty_option_for_signal
from src.fyers_integration import FyersIntegration
from src.config_manager import ConfigManager
from src.calendar_loader import get_nse_trading_calendar_for_current_year
from src.premarket.services import OptionChainService
from src.expiry_utils import fyers_nifty_option_symbol



IST = pytz.timezone("Asia/Kolkata")


# ──────────────────────────────────────────────────────────────
# Global trading state
# ──────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

TRADING_ENABLED = True  # in-memory switch
CURRENT_SESSION_TOKEN: str | None = None  # track single active session
TRADING_CALENDAR = get_nse_trading_calendar_for_current_year()
OPTION_CHAIN_CACHE = {"ts": 0.0, "data": None}
_OPTION_CHAIN_LOCK = threading.Lock()
_OPTION_CHAIN_INFLIGHT = False
_OPTION_CHAIN_LAST_ERROR_TS = 0.0


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
premarket_analyzer = PremarketAnalyzer(symbol="NIFTY")
option_chain_service = OptionChainService(symbol="NIFTY")
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
    

# ──────────────────────────────────────────────────────────────
# Signal building helpers
# ──────────────────────────────────────────────────────────────

def parse_simple_alert(text: str):
    """
    Expect patterns like:
        BUY_CALL_BREAKOUT
        BUY_CALL_REVERSAL
        BUY_PUT_BREAKOUT
        BUY_PUT_REVERSAL
        SELL_CALL_EXIT   (for exits – you can expand later)

    Returns (side, direction, setup_type) or (None, None, None) if unknown.
    """
    t = (text or "").strip().upper()

    # you can make this more fancy later
    if t in ("BUY_CALL_BREAKOUT", "BUY_CE_BREAKOUT"):
        return "buy", "LONG_CALL", "BREAKOUT"
    if t in ("BUY_CALL_REVERSAL", "BUY_CE_REVERSAL"):
        return "buy", "LONG_CALL", "REVERSAL"

    if t in ("BUY_PUT_BREAKOUT", "BUY_PE_BREAKOUT"):
        return "buy", "LONG_PUT", "BREAKOUT"
    if t in ("BUY_PUT_REVERSAL", "BUY_PE_REVERSAL"):
        return "buy", "LONG_PUT", "REVERSAL"
    if t in ("SELL_CALL_EXIT", "SELL_CE_EXIT", "EXIT_CALL", "EXIT_CE"):
        return "sell", "LONG_CALL", "EXIT"
    if t in ("SELL_PUT_EXIT", "SELL_PE_EXIT", "EXIT_PUT", "EXIT_PE"):
        return "sell", "LONG_PUT", "EXIT"
    return None, None, None


def fetch_nifty_spot_price() -> float:
    """Fetch current NIFTY spot price via Fyers quotes API."""
    cached = get_cached_option_chain()
    if cached:
        try:
            underlying = cached.get("records", {}).get("underlyingValue") or cached.get("filtered", {}).get("data", [{}])[0].get("underlyingValue")
            if underlying:
                return float(underlying)
        except Exception:
            pass
    fyers = fyers_integration.get_fyers_instance()
    resp = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
    if resp.get("s") != "ok":
        raise RuntimeError(f"Error fetching NIFTY quote: {resp}")

    data = resp.get("d") or []
    v = data[0].get("v", {}) if data else {}
    lp = v.get("lp")
    if lp is None:
        raise RuntimeError(f"Last price missing in quote payload: {resp}")
    return float(lp)


def _refresh_option_chain_async():
    """
    Fire-and-forget refresh of NSE option chain. Does not block webhook.
    """
    global _OPTION_CHAIN_INFLIGHT, _OPTION_CHAIN_LAST_ERROR_TS
    with _OPTION_CHAIN_LOCK:
        if _OPTION_CHAIN_INFLIGHT:
            return
        _OPTION_CHAIN_INFLIGHT = True

    def _worker():
        global _OPTION_CHAIN_INFLIGHT, _OPTION_CHAIN_LAST_ERROR_TS
        try:
            data = option_chain_service._fetch_raw()
            OPTION_CHAIN_CACHE["data"] = data
            OPTION_CHAIN_CACHE["ts"] = time.time()
        except Exception as exc:
            now = time.time()
            if now - _OPTION_CHAIN_LAST_ERROR_TS > 300:
                logger.warning("Option chain refresh failed: %s", exc)
                _OPTION_CHAIN_LAST_ERROR_TS = now
            else:
                logger.debug("Option chain refresh failed (suppressed): %s", exc)
        finally:
            _OPTION_CHAIN_INFLIGHT = False

    threading.Thread(target=_worker, daemon=True).start()


def get_cached_option_chain(max_age_sec: int = 30):
    """
    Return cached option-chain data if fresh enough.
    If stale/missing, trigger async refresh and return last known (may be None).
    """
    now = time.time()
    cached = OPTION_CHAIN_CACHE.get("data")
    ts = OPTION_CHAIN_CACHE.get("ts") or 0
    age = now - ts
    if cached and age <= max_age_sec:
        return cached
    # trigger refresh without blocking
    _refresh_option_chain_async()
    return cached


# Warm the option-chain cache at startup (best-effort, non-blocking)
_refresh_option_chain_async()


def _schedule_option_chain_refresh(interval_sec: int = 20):
    """
    Periodically refresh option chain so webhook paths remain warm.
    """
    def _tick():
        _refresh_option_chain_async()
        _schedule_option_chain_refresh(interval_sec)

    t = threading.Timer(interval_sec, _tick)
    t.daemon = True
    t.start()


_schedule_option_chain_refresh()

def choose_nifty_option_from_signal(
    spot_price: float,
    direction: str,
    setup_type: str,
    calendar=None,
):
    now_ist = datetime.now(IST)
    calendar = calendar or TRADING_CALENDAR

    selection = select_nifty_option_for_signal(
        now_ist=now_ist,
        spot_price=spot_price,
        direction=direction,   # "LONG_CALL" / "LONG_PUT"
        setup_type=setup_type, # "BREAKOUT" / "REVERSAL"
        calendar=calendar,
        expiry_weekday=1,      # NIFTY weekly expiry (legacy Tuesday expectation)
        strike_step=50,
        underlying="NIFTY",
        exchange_prefix="NSE:",
    )
    return selection


def select_option_from_chain(
    *,
    spot_price: float,
    direction: str,
    setup_type: str,
    calendar,
    strike_step: int = 50,
    search_window: int = 200,
) -> OptionSelection | None:
    """
    Pick the most liquid strike near ATM using NSE option chain OI/volume.
    Falls back to calendar-based selection if anything goes wrong.
    """
    raw = get_cached_option_chain()
    if not raw:
        return None

    filtered = raw.get("filtered", {}) or {}
    rows = filtered.get("data") or raw.get("records", {}).get("data", [])
    expiry_str = filtered.get("expiryDate") or (raw.get("records", {}).get("expiryDates") or [None])[0]

    if not rows or not expiry_str:
        logger.warning("Option chain missing rows/expiry; falling back to calendar selection.")
        return None

    try:
        expiry_d = datetime.strptime(expiry_str, "%d-%b-%Y").date()
    except Exception as exc:
        logger.warning("Unable to parse option-chain expiry %r: %s", expiry_str, exc)
        return None

    atm = int(round(spot_price / strike_step) * strike_step)
    lower = atm - search_window
    upper = atm + search_window
    leg_key = "CE" if direction == "LONG_CALL" else "PE"

    best = None
    for row in rows:
        strike = row.get("strikePrice")
        if strike is None or strike < lower or strike > upper:
            continue
        leg = row.get(leg_key)
        if not leg:
            continue

        oi = float(leg.get("openInterest") or 0)
        vol = float(leg.get("totalTradedVolume") or 0)
        ltp = float(leg.get("lastPrice") or 0)
        score = oi * 0.7 + vol * 0.3  # liquidity + participation

        if best is None or score > best["score"]:
            best = {"strike": int(strike), "oi": oi, "vol": vol, "ltp": ltp, "score": score}

    if not best:
        logger.warning("Option chain scan produced no candidates; falling back to calendar selection.")
        return None

    opt_type = "CE" if direction == "LONG_CALL" else "PE"
    symbol = fyers_nifty_option_symbol(
        underlying="NIFTY",
        expiry_d=expiry_d,
        strike=best["strike"],
        opt_type=opt_type,
        calendar=calendar,
        expiry_weekday=1,
        exchange_prefix="NSE:",
    )

    notes = (
        f"OI-based strike={best['strike']} ({leg_key}) oi={best['oi']:.0f} "
        f"vol={best['vol']:.0f} ltp={best['ltp']:.2f} expiry={expiry_d}"
    )

    return OptionSelection(
        direction=direction,
        setup_type=setup_type,
        strike_style="ATM",
        strike=best["strike"],
        expiry_date=expiry_d,
        symbol=symbol,
        notes=notes,
    )


def build_order_details_from_signal(
    direction: str,
    setup_type: str,
    spot_price: float | None = None,
    side: str = "buy"):
    selection_setup = setup_type if setup_type in ("BREAKOUT", "REVERSAL") else "BREAKOUT"
    # Try cached option-chain underlying to avoid an extra quote call
    raw_chain = get_cached_option_chain()
    if spot_price is None and raw_chain:
        try:
            spot_price = float(
                raw_chain.get("records", {}).get("underlyingValue")
                or raw_chain.get("filtered", {}).get("data", [{}])[0].get("underlyingValue")
            )
        except Exception:
            spot_price = None
    if spot_price is None:
        spot_price = fetch_nifty_spot_price()

    selection = select_option_from_chain(
        spot_price=spot_price,
        direction=direction,
        setup_type=selection_setup,
        calendar=TRADING_CALENDAR,
    )

    source = "option_chain"
    if selection is None:
        selection = choose_nifty_option_from_signal(
            spot_price=spot_price,
            direction=direction,
            setup_type=selection_setup,
            calendar=TRADING_CALENDAR,
        )
        source = "calendar"

    opt_type = "CE" if direction == "LONG_CALL" else "PE"
    fyers_side = 1 if str(side).lower() == "buy" else -1

    order_details = {
        "symbol": selection.symbol,
        "qty": 75,               # you can parameterize this
        "type": 2,               # MARKET
        "side": fyers_side,      # 1 = buy, -1 = sell
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "stopLoss": 0,
        "takeProfit": 0,
        "optType": opt_type,
        "optStrike": str(selection.strike),
    }

    return order_details, f"{selection.notes} | source={source}"

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
    payload = f'<div class="log-container">{html_body}</div>'
    return app.response_class(payload, mimetype="text/html")


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
    # Try JSON first (legacy Pine webhook)
    data = request.get_json(force=True, silent=True)
    raw_body = request.data.decode("utf-8", errors="ignore")

    logging.info(f"Incoming webhook raw: {raw_body}")
    logging.info(f"Incoming webhook json: {data}")

    if not TRADING_ENABLED:
        logging.info("Webhook received but TRADING_ENABLED is False. Ignoring order.")
        return jsonify({"status": "ignored", "reason": "trading_paused"}), 200

    # ───────────── Legacy mode: Pine sends full JSON ─────────────
    if isinstance(data, dict) and data.get("event") == "place_order":
        order_details = data.get("order_details", {})

        side = order_details.get("side")
        symbol = order_details.get("symbol")
        opt_strike = str(order_details.get("optStrike", "") or "").strip()
        opt_type = str(order_details.get("optType", "") or "").upper()

        if not side or not symbol:
            return jsonify({"status": "invalid_order_details"}), 400

        # If legacy payload is the index (no strike), build option selection first.
        needs_selection = (
            symbol.upper().endswith("NIFTY50-INDEX")
            or symbol.upper().endswith("NIFTY 50")
            or opt_strike == ""
        )

        if needs_selection:
            opt_type = opt_type if opt_type in {"CE", "PE"} else "CE"
            direction = "LONG_CALL" if opt_type == "CE" else "LONG_PUT"
            try:
                order_details, note = build_order_details_from_signal(
                    direction=direction,
                    setup_type="BREAKOUT",
                    side=side,
                )
                logging.info(f"[LEGACY->PY] Built option from signal: {note}")
            except Exception as exc:
                logger.error(f"Legacy payload failed to build option: {exc}")
                return jsonify({"status": "error", "detail": str(exc)}), 500
        else:
            if side == "buy":
                order_details["side"] = 1
            elif side == "sell":
                order_details["side"] = -1

       

        logging.info(f"[LEGACY] Order details: {order_details}")
        dispatch_order(order_details)
        return jsonify({"status": "order_processing_initiated", "mode": "legacy"}), 200

    # ───────────── New mode: simple alert text ─────────────
    alert_text = raw_body.strip()
    side, direction, setup_type = parse_simple_alert(alert_text)

    logging.info(f"debug1 :{alert_text}")
    logging.info(f"debug2 :{side}, {direction}, {setup_type}")

    if not side or not direction or not setup_type:
        return jsonify({"status": "ignored", "reason": "unrecognized_alert", "alert": alert_text}), 200

    try:
        order_details, note = build_order_details_from_signal(direction, setup_type, side=side)
        logging.info(f"debug3 :{order_details}")
        logging.info(f"debug4 :{note}")
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

   

    logging.info(f"[PY-SIGNAL] {note}")
    logging.info(f"[PY-SIGNAL] Order details: {order_details}")

    dispatch_order(order_details)
    return jsonify({"status": "order_processing_initiated", "mode": "python_signal"}), 200



def place_order(order_details):
    try:
        logging.info(f"Placing order via Fyers: {order_details}")
        fyers = fyers_integration.get_fyers_instance()
        response = fyers.place_order(order_details)
        logging.info(f"Order response: {response}")
    except Exception as e:
        logging.error(f"Order placement error: {e}")


def dispatch_order(order_details):
    """
    Fire-and-forget wrapper so webhook responses are instant.
    """
    threading.Thread(target=lambda: place_order(order_details), daemon=True).start()


# ──────────────────────────────────────────────────────────────
# Routes: Restart + Pause/Resume trading
# ──────────────────────────────────────────────────────────────

@app.route("/restart", methods=["GET", "POST"])
@login_required
def restart_service():
    token = request.form.get("token") or request.args.get("token")
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
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
        logger.error(f"Premarket analysis failed: {exc}")
        return jsonify({"error": "Premarket analysis failed", "detail": str(exc)}), 503

    return jsonify(data), 200


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
