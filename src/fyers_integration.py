# fyers_integration.py

import os
import webbrowser
import logging
from fyers_apiv3 import fyersModel
from src.config_manager import AppConfig, ConfigManager

logger = logging.getLogger(__name__)

class FyersIntegration:
    def __init__(self):
        self.config: AppConfig = ConfigManager.load_config()

    def generate_auth_url(self):
        session = fyersModel.SessionModel(
            client_id=self.config.client_id,
            redirect_uri=self.config.redirect_uri,
            response_type="code",
            state="sample",
            secret_key=self.config.secret_key,
            grant_type="authorization_code",
        )
        auth_url = session.generate_authcode()
        if os.getenv("FYERS_NO_BROWSER", "").lower() in {"1", "true", "yes", "on"}:
            logging.info(f"Headless mode: open this URL in a browser to authorize:")
            logging.info(f"Authorization URL: {auth_url}")
        else:
            webbrowser.open(auth_url)
            logging.info(f"Authorization URL: {auth_url}")

    def fetch_access_token(self, auth_code: str):
        session = fyersModel.SessionModel(
            client_id=self.config.client_id,
            redirect_uri=self.config.redirect_uri,
            response_type="code",
            state="sample",
            secret_key=self.config.secret_key,
            grant_type="authorization_code",
        )
        session.set_token(auth_code)
        response = session.generate_token()
        logging.info(f"Access Token Response: Access token generated")
        if "access_token" in response:
            self.config.access_token = response["access_token"]
            ConfigManager.save_config(self.config)
            logging.info("Fyers authentication successful – ready for trading.")
            try:
                fy = self.get_fyers_instance()
                profile = fy.get_profile()
                logging.info(f"Fyers connection verified. Profile response: {profile}")
            except Exception as e:
                logging.error(f"Fyers connection verification failed: {e}")
        else:
            logging.error("Error fetching access token.")

    def get_fyers_instance(self):
        if not self.config.access_token:
            raise ValueError("Access token is missing. Authenticate first.")
        logging.debug("Creating FyersModel instance with stored access token.")
        return fyersModel.FyersModel(
            token=self.config.access_token,
            is_async=False,
            client_id=self.config.client_id
        )

