# webhook_app.py

import logging
import asyncio
import os
from flask import Flask, request, jsonify, abort
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




def configure_logging():
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

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


@app.before_request
def allow_specific_routes():
    # Allow POST /webhook
    if request.method == 'POST' and request.path == '/webhook':
        return  # Proceed

    # Allow GET /capture_auth_code
    if request.method == 'GET' and '/capture_auth_code' in request.path:
        return  # Proceed

    # Block everything else
    abort(403)

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
