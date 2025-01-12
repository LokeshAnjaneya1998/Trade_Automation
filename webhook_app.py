# webhook_app.py

import logging
import asyncio
from flask import Flask, request, jsonify
from fyers_integration import FyersIntegration
from config_manager import ConfigManager

app = Flask(__name__)
fyers_integration = FyersIntegration()

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
    logging.info(f"Raw data from TradingView: {data}")

    if not data or "event" not in data:
        return jsonify({"status": "Invalid webhook data"}), 400

    if data["event"] == "place_order":
        order_details = data.get("order_details", {})
        asyncio.run(place_order(order_details))
        return jsonify({"status": "Order processing initiated."})

    return jsonify({"status": "No valid event found"}), 400

async def place_order(order_details):
    try:
        fyers = fyers_integration.get_fyers_instance()
        response = fyers.place_order(order_details)
        logging.info(f"Order response: {response}")
    except Exception as e:
        logging.error(f"Order placement error: {e}")

def run_app():
    logging.basicConfig(level=logging.INFO)
    # Generate login URL if not authorized yet
    fyers_integration.generate_auth_url()
    app.run(host="0.0.0.0", port=5000)

if __name__ == "__main__":
    run_app()