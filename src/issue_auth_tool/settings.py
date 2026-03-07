from tomllib import load
from typing import cast

from .types import AppConfig, SettingConfig

with open('config.toml', 'rb') as f:
    config: AppConfig = cast(AppConfig, load(f))
setting: SettingConfig = config['settings']
