"""
Early mocking to prevent module-level imports from making real network calls.
This runs before test collection so that sys.modules is pre-populated.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _fake_module(name: str, **attrs: object) -> ModuleType:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


# github.Auth.Token needs to exist
_fake_auth = _fake_module("github.Auth", Token=MagicMock)
sys.modules.setdefault(
    "github", _fake_module("github", Github=MagicMock, Auth=_fake_auth)
)
sys.modules.setdefault("github.Auth", _fake_auth)
sys.modules.setdefault("openai", _fake_module("openai", OpenAI=MagicMock))

# Mock uniinfo_editor BEFORE the viewer module imports it
_fake_tui_instance = MagicMock()
sys.modules.setdefault(
    "uniinfo_editor",
    _fake_module(
        "uniinfo_editor", UniInfoTUI=MagicMock(return_value=_fake_tui_instance)
    ),
)
