import logging
from fyers_apiv3 import fyersModel
import asyncio
from flask import Flask, request, jsonify
import webbrowser
import json

# Flask app
app = Flask(__name__)

class Config:
    CONFIG_FILE = "config.json"

    @staticmethod
    def load_config():
        with open(Config.CONFIG_FILE, "r") as file:
            return json.load(file)

    @staticmethod
    def save_config(data):
        with open(Config.CONFIG_FILE, "w") as file:
            json.dump(data, file, indent=4)

class FyersIntegration:
    def __init__(self):
        self.config = Config.load_config()
        self.client_id = self.config["client_id"]
        self.redirect_uri = self.config["redirect_uri"]
        self.secret_key = self.config["secret_key"]
        self.auth_code = self.config.get("auth_code", None)
        self.access_token = self.config.get("access_token", None)

    def generate_auth_url(self):
        session = fyersModel.SessionModel(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            response_type="code",
            state="sample",
            secret_key=self.secret_key,
            grant_type="authorization_code",
        )
        auth_url = session.generate_authcode()
        webbrowser.open(auth_url)
        print(f"Authorization URL: {auth_url}")

    def fetch_access_token(self, auth_code):
        session = fyersModel.SessionModel(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            response_type="code",
            state="sample",
            secret_key=self.secret_key,
            grant_type="authorization_code",
        )
        session.set_token(auth_code)
        response = session.generate_token()
        print(f"API Response: {response}")  # Debugging: Print the entire response
        try:
            if "access_token" in response:
                self.access_token = response["access_token"]
                self.config["access_token"] = self.access_token
                Config.save_config(self.config)
                print("Access token retrieved and saved.")
            else:
                print(f"Error fetching access token: {response.get('message', 'Unknown error')}")
        except Exception as e:
            print(f"Unexpected error: {e}")


    def get_fyers_instance(self):
        if not self.access_token:
            raise ValueError("Access token is missing. Authenticate first.")
        return fyersModel.FyersModel(token=self.access_token, is_async=False, client_id=self.client_id)

fyers_integration = FyersIntegration()

@app.route("/capture_auth_code", methods=["GET"])
def capture_auth_code():
    auth_code = request.args.get("auth_code")
    state = request.args.get("state")

    if not auth_code:
        return "Authorization code not found in the request.", 400

    # Update the config with the new auth_code
    fyers_integration.config["auth_code"] = auth_code
    Config.save_config(fyers_integration.config)
    print(f"Authorization code captured: {auth_code}")

    # Automatically fetch access token
    fyers_integration.fetch_access_token(auth_code)
    return "Authorization code captured and access token generated successfully. You can close this tab."

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.info(f"Webhook received: {data}")

    if not data or "event" not in data:
        return jsonify({"status": "Invalid webhook data."}), 400

    if data["event"] == "place_order":
        order_details = data.get("order_details", {})
        asyncio.run(place_order(order_details))
        return jsonify({"status": "Order processing initiated."})

    return jsonify({"status": "No valid event found."}), 400

async def place_order(order_details):
    try:
        fyers = fyers_integration.get_fyers_instance()
        response = fyers.place_order(order_details)
        logging.info(f"Order response: {response}")
    except Exception as e:
        logging.error(f"Error placing order: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("No authorization code found. Generating login URL...")
    fyers_integration.generate_auth_url()

    app.run(host="0.0.0.0", port=5000)

##lt --port 5000 --subdomain lokeshfyerstestdomain