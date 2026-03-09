from typing import NotRequired, TypedDict


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
    workers: NotRequired[int]
    prompt_type: str
    prompt_judgement: str
    google_query: str
    mcp: MCPConfig


class AppConfig(TypedDict):
    secret: SecretConfig
    settings: SettingConfig

class PostData(TypedDict):
    title: str
    num: int
    text: str
