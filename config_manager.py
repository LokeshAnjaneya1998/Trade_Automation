# config_manager.py

import json
from dataclasses import dataclass
from pathlib import Path

@dataclass
class AppConfig:
    client_id: str
    redirect_uri: str
    secret_key: str
    auth_code: str = None
    access_token: str = None

class ConfigManager:
    # Resolve config path relative to this file so it works on Windows/Linux
    CONFIG_FILE = Path(__file__).resolve().parent / "configandlogs" / "config.json"

    @staticmethod
    def load_config() -> AppConfig:
        with open(ConfigManager.CONFIG_FILE, "r") as file:
            data = json.load(file)
        return AppConfig(**data)

    @staticmethod
    def save_config(config: AppConfig):
        with open(ConfigManager.CONFIG_FILE, "w") as file:
            json.dump(config.__dict__, file, indent=4)

