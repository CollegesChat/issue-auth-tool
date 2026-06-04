from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

PostSource = Literal["issues", "discussions"]
PostKey = str

type MCPCommandName = Literal[
    "outdated", "evil", "alias"
]  # TODO: see astral-sh/ty#3661


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


type LabelsConfig = dict[MCPCommandName, str]


class SettingConfig(TypedDict):
    type: list[str]
    rate_per_minute: int
    workers: NotRequired[int]
    dry_run: NotRequired[bool]
    prompt_type: str
    prompt_judgement: str
    google_query: str
    mcp: MCPConfig
    labels: NotRequired[LabelsConfig]


class AppConfig(TypedDict):
    secret: SecretConfig
    settings: SettingConfig


class PostData(TypedDict):
    title: str
    num: int
    text: str
    source: NotRequired[PostSource]


@dataclass(slots=True)
class DeferredPost:
    post: PostData
    ret_text: str


class ValidReport(TypedDict):
    type: MCPCommandName
    reason: str
    mcp: list[str]
    source: NotRequired[PostSource]


class _LLMPromptType(TypedDict):
    type: MCPCommandName
    reason: str
    mcp: list[str]


type LLMPromptType = _LLMPromptType | None

type LLMPromptJudgement = list[str] | None
