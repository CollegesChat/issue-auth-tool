from tomllib import load
from typing import TypedDict, cast


class LLMConfig(TypedDict):
    key: str
    server: str
    model: str


class SecretConfig(TypedDict):
    GITHUB_TOKEN: str
    OWNER: str
    REPO_NAME: str
    llm: LLMConfig


class GoogleMCPConfig(TypedDict):
    cx: str
    key: str


class ViewerMCPConfig(TypedDict):
    config: str


class MCPConfig(TypedDict):
    google: GoogleMCPConfig
    viewer: ViewerMCPConfig


class SettingConfig(TypedDict):
    type: list[str]
    rate_per_minute: int
    prompt_type: str
    prompt_judgement: str
    google_query: str
    mcp: list[MCPConfig]


class AppConfig(TypedDict):
    secret: SecretConfig
    settings: SettingConfig

with open('config.toml', 'rb') as f:
    config: AppConfig = cast(AppConfig, load(f))
setting: SettingConfig = config['settings']
