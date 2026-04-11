"""
Early mocking to prevent module-level imports from making real network calls.
This runs before test collection so that sys.modules is pre-populated.
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

# github.Auth.Token needs to exist
_fake_auth = MagicMock()
sys.modules.setdefault("github", SimpleNamespace(Github=MagicMock, Auth=_fake_auth))
sys.modules.setdefault("github.Auth", _fake_auth)
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=MagicMock))

# Mock uniinfo_editor BEFORE the viewer module imports it
_fake_tui_instance = MagicMock()
sys.modules.setdefault(
    "uniinfo_editor",
    SimpleNamespace(UniInfoTUI=MagicMock(return_value=_fake_tui_instance)),
)

# Pre-mock the viewer module itself so imports don't hit the config bug
# (setting['mcp'] is a list in _config.toml, but viewer.py expects a dict)
_fake_viewer_helper = MagicMock()
sys.modules.setdefault(
    "issue_auth_tool.mcp.viewer",
    SimpleNamespace(helper=_fake_viewer_helper, view=MagicMock()),
)
