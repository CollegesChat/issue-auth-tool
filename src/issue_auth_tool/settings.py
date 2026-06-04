import os
from tomllib import load
from typing import cast

from .types import AppConfig, SettingConfig

config_path = os.environ.get("CONFIG_FILE", "config.toml")
with open(config_path, "rb") as f:
    config: AppConfig = cast(AppConfig, load(f))
setting: SettingConfig = config["settings"]
