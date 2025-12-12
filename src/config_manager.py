# src/config_manager.py (example pattern)

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Persist config next to other logs/config artifacts
CONFIG_PATH = Path(__file__).resolve().parents[1] / "configandlogs" / "config.json"


@dataclass
class WebLoginConfig:
    username: str = ""
    password: str = ""


@dataclass
class AppConfig:
    client_id: str = ""
    redirect_uri: str = ""
    secret_key: str = ""
    access_token: str = ""
    refresh_token: str = ""
    auth_code: str = ""
    web_login: WebLoginConfig = field(default_factory=WebLoginConfig)


class ConfigManager:
    def __init__(self, path: Path = CONFIG_PATH):
        self._path = path
        self.config: AppConfig = self._load_config()

    def _load_config(self) -> AppConfig:
        if not self._path.exists():
            raise FileNotFoundError(
                f"Config file not found . Create it before running."
            )

        with open(self._path, "r", encoding="utf-8") as f:
            data = json.load(f)

        web_login_raw = data.get("web_login", {}) or {}
        web_login = WebLoginConfig(
            username=web_login_raw.get("username", ""),
            password=web_login_raw.get("password", ""),
        )

        cfg = AppConfig(
            client_id=data.get("client_id", ""),
            redirect_uri=data.get("redirect_uri", ""),
            secret_key=data.get("secret_key", ""),
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            auth_code=data.get("auth_code", ""),
            web_login=web_login,
        )
        logger.debug("Loaded config")
        return cfg

    def save_config(self, config: Optional[AppConfig] = None) -> None:
        """Persist the config to disk."""
        cfg = config or self.config
        payload = asdict(cfg)
        payload["web_login"] = asdict(cfg.web_login)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        logger.info("Saved config")
