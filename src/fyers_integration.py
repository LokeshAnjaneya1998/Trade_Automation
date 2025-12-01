# fyers_integration.py

import logging
import os
import webbrowser
from fyers_apiv3 import fyersModel
from src.config_manager import ConfigManager

logger = logging.getLogger(__name__)


class FyersIntegration:
    def __init__(self):
        self.config_manager = ConfigManager()
        self.config = self.config_manager.config

    def refresh_config(self) -> None:
        """Reload config from disk so new tokens are picked up by other instances."""
        self.config_manager = ConfigManager()
        self.config = self.config_manager.config

    def _build_session(self) -> fyersModel.SessionModel:
        missing = [
            name
            for name, value in [
                ("client_id", self.config.client_id),
                ("redirect_uri", self.config.redirect_uri),
                ("secret_key", self.config.secret_key),
            ]
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required config values: {', '.join(missing)}")

        return fyersModel.SessionModel(
            client_id=self.config.client_id,
            redirect_uri=self.config.redirect_uri,
            response_type="code",
            state="sample",
            secret_key=self.config.secret_key,
            grant_type="authorization_code",
        )

    def generate_auth_url(self) -> None:
        session = self._build_session()
        auth_url = session.generate_authcode()
        if os.getenv("FYERS_NO_BROWSER", "").lower() in {"1", "true", "yes", "on"}:
            logger.info("Headless mode: open this URL in a browser to authorize:")
            logger.info(f"Authorization URL: {auth_url}")
        else:
            webbrowser.open(auth_url)
            logger.info(f"Authorization URL: {auth_url}")

    def fetch_access_token(self, auth_code: str) -> None:
        session = self._build_session()
        session.set_token(auth_code)
        response = session.generate_token()
        logger.info("Access Token Response: Access token generated")

        access_token = response.get("access_token") if isinstance(response, dict) else None
        if access_token:
            self.config.access_token = access_token
            self.config.auth_code = auth_code
            self.config_manager.save_config(self.config)
            logger.info("Fyers authentication successful - ready for trading.")
            try:
                fy = self.get_fyers_instance()
                profile = fy.get_profile()
                logger.info(f"Fyers connection verified. Profile response: {profile}")
            except Exception as e:
                logger.error(f"Fyers connection verification failed: {e}")
        else:
            logger.error(f"Error fetching access token. Response: {response}")

    def get_fyers_instance(self) -> fyersModel.FyersModel:
        # Reload config each time to pick up fresh access_token saved by other code paths.
        self.refresh_config()
        if not self.config.access_token:
            raise ValueError("Access token is missing. Authenticate first.")

        logger.debug("Creating FyersModel instance with stored access token.")
        return fyersModel.FyersModel(
            token=self.config.access_token,
            is_async=False,
            client_id=self.config.client_id,
        )
