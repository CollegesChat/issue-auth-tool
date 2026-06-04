"""
Tests for the three-phase LLM workflow in the issue processing pipeline.

Phase 1: First type detection — LLM identifies issue type and generates MCP instructions.
Phase 2: Second judgement — LLM verifies the type with MCP context and returns final commands.
Phase 3: Command execution & generate — execute commands and produce changelog.

Testing strategy:
- MUST mock: get_llm_response (controls LLM output deterministically)
- SHOULD mock: fetch_issues_and_discussions (provides controlled test posts)
- Minimize mocks: let process_report run real logic, only mock side effects
  (helper.do_del / do_outdate / do_alias / do_generate)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_auth_tool.types import PostData, ValidReport

# ──────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────

TEST_POST: PostData = {
    "title": "Test University Discussion",
    "num": 42,
    "text": "Is the information about 西安电子科技大学 still accurate?",
}


def _make_llm_type_response(
    type_: str, reason: str = "test reason", mcp: list | None = None
) -> str:
    """Build a valid LLM response for the first type-detection prompt."""
    if mcp is None and type_ != "invalid":
        mcp = ["view 1234"]
    if mcp is None:
        mcp = []
    return json.dumps({"type": type_, "reason": reason, "mcp": mcp}, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────
# Phase 1: First type detection
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "llm_type,mcp,expect_in_reports",
    [
        ("outdated", ["view 1234"], True),
        ("evil", ["view 5678", "google 北京大学 清华大学"], True),
        ("alias", ["view 999"], True),
        ("invalid", [], False),
    ],
)
def test_first_type_detection(llm_type, mcp, expect_in_reports):
    """
    Phase 1: First LLM call identifies issue type and generates MCP instructions.

    - Mock get_llm_response to return a JSON with the given type.
    - Mock fetch_issues_and_discussions to yield a single test post.
    - Verify that valid types (outdated/evil/alias) are saved to
      all_valid_reports, while 'invalid' is NOT saved.
    """
    from issue_auth_tool.issues_auth_tool import (
        all_valid_reports,
        process_post,
    )

    # Reset shared state
    all_valid_reports.clear()

    llm_output = _make_llm_type_response(llm_type, f"test reason for {llm_type}", mcp)

    with (
        patch(
            "issue_auth_tool.issues_auth_tool.get_llm_response", return_value=llm_output
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.db_path",
            MagicMock(),
        ),
    ):
        process_post(TEST_POST, prompt_on_failure=True)

    if expect_in_reports:
        assert "unknown-42" in all_valid_reports
        report = all_valid_reports["unknown-42"]
        assert report["type"] == llm_type
        assert report["reason"] == f"test reason for {llm_type}"
        assert report["mcp"] == mcp
    else:
        assert "unknown-42" not in all_valid_reports


def test_source_aware_reports_do_not_overwrite_same_number():
    """
    Phase 1: Issue and discussion reports with the same number use different keys.
    """
    from issue_auth_tool.issues_auth_tool import all_valid_reports, process_post

    all_valid_reports.clear()
    llm_output = _make_llm_type_response("evil", "same number", ["view 1"])
    issue_post = PostData(title="Issue", num=42, text="bad data", source="issues")
    discussion_post = PostData(
        title="Discussion", num=42, text="bad data", source="discussions"
    )

    with (
        patch(
            "issue_auth_tool.issues_auth_tool.get_llm_response",
            return_value=llm_output,
        ),
        patch("issue_auth_tool.issues_auth_tool.save_post_output"),
    ):
        process_post(issue_post, prompt_on_failure=True)
        process_post(discussion_post, prompt_on_failure=True)

    assert set(all_valid_reports) == {"issues-42", "discussions-42"}
    assert all_valid_reports["issues-42"]["source"] == "issues"
    assert all_valid_reports["discussions-42"]["source"] == "discussions"


def test_processed_post_keys_support_new_and_legacy_database_names():
    """
    Phase 1: New source-aware files are exact; legacy numeric files skip both sources.
    """
    from issue_auth_tool.issues_auth_tool import parse_processed_post_keys

    keys = parse_processed_post_keys(
        [
            Path("database/issues-1.json"),
            Path("database/discussions-2.json"),
            Path("database/42.json"),
        ]
    )

    assert keys == {"issues-1", "discussions-2", "issues-42", "discussions-42"}


def test_first_type_detection_invalid_json_deferred():
    """
    Phase 1: When LLM returns invalid JSON, process_post should return a DeferredPost.
    """
    from issue_auth_tool.issues_auth_tool import (
        all_valid_reports,
        process_post,
    )

    all_valid_reports.clear()

    bad_post: PostData = {
        "title": "Bad JSON Post",
        "num": 99,
        "text": "Some content.",
    }

    with (
        patch(
            "issue_auth_tool.issues_auth_tool.get_llm_response",
            return_value="not valid json!!!",
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.prompt_manual_fix",
            return_value=None,
        ),
    ):
        deferred = process_post(bad_post, prompt_on_failure=False)
        assert deferred is not None
        assert deferred.post["num"] == 99
        assert deferred.ret_text == "not valid json!!!"


# ──────────────────────────────────────────────────────────────
# Phase 2: Second judgement verification
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "judgement_output,expect_cmd_prefix",
    [
        ('["del 1"]', "del"),
        ('["outdate 北京大学"]', "outdate"),
        ('["alias 旧大学 新大学 1"]', "alias"),
        ("null", None),
    ],
)
def test_second_judgement(judgement_output, expect_cmd_prefix):
    """
    Phase 2: Second LLM judgement call verifies type with MCP context
    and returns final commands.

    - Set up a ValidReport in all_valid_reports.
    - Mock get_llm_response to return the judgement output.
    - Mock handle_instruction to return fake MCP context.
    - Mock _execute_final_command to capture calls.
    - Verify the correct command is executed (or none for null).
    """
    from issue_auth_tool.issues_auth_tool import (
        all_valid_reports,
        process_report,
    )

    all_valid_reports.clear()
    all_valid_reports["issues-42"] = ValidReport(
        type="evil",
        reason="malicious content detected",
        mcp=["view 1234"],
        source="issues",
    )

    executed_commands: list[dict] = []

    def fake_execute(cmd: str, issue_id: int) -> None:
        executed_commands.append({"cmd": cmd, "issue_id": issue_id})

    with (
        patch(
            "issue_auth_tool.issues_auth_tool.get_llm_response",
            return_value=judgement_output,
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.handle_instruction",
            return_value="fake mcp context result",
        ),
        patch(
            "issue_auth_tool.issues_auth_tool._execute_final_command",
            side_effect=fake_execute,
        ),
    ):
        process_report("issues-42", all_valid_reports["issues-42"])

    if expect_cmd_prefix is not None:
        assert len(executed_commands) == 1
        assert executed_commands[0]["issue_id"] == 42
        assert executed_commands[0]["cmd"].startswith(expect_cmd_prefix)
    else:
        assert len(executed_commands) == 0


def test_second_judgement_invalid_output_logged():
    """
    Phase 2: When LLM judgement output is invalid, it should be logged
    as an error and no command executed.
    """
    from issue_auth_tool.issues_auth_tool import (
        all_valid_reports,
        process_report,
    )

    all_valid_reports.clear()
    all_valid_reports["issues-42"] = ValidReport(
        type="evil",
        reason="test",
        mcp=["view 1"],
        source="issues",
    )

    executed_commands: list[dict] = []

    def fake_execute(cmd: str, issue_id: int) -> None:
        executed_commands.append({"cmd": cmd, "issue_id": issue_id})

    with (
        patch(
            "issue_auth_tool.issues_auth_tool.get_llm_response",
            return_value="this is not valid judgement",
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.handle_instruction",
            return_value="some context",
        ),
        patch(
            "issue_auth_tool.issues_auth_tool._execute_final_command",
            side_effect=fake_execute,
        ),
        patch("issue_auth_tool.issues_auth_tool.logger") as mock_logger,
    ):
        process_report("issues-42", all_valid_reports["issues-42"])

    assert len(executed_commands) == 0
    assert mock_logger.error.called


def test_evil_issue_labeled_after_confirmed_judgement():
    """
    Phase 2: Confirmed evil issue reports should receive the evil-data label.
    """
    from issue_auth_tool.issues_auth_tool import process_report
    from issue_auth_tool.types import ValidReport

    report = ValidReport(
        type="evil",
        reason="malicious content detected",
        mcp=["view 1234"],
        source="issues",
    )

    with (
        patch(
            "issue_auth_tool.issues_auth_tool.get_llm_response",
            return_value='["del 1"]',
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.handle_instruction",
            return_value="fake mcp context result",
        ),
        patch("issue_auth_tool.issues_auth_tool._execute_final_command"),
        patch("issue_auth_tool.issues_auth_tool.label_evil_issue") as mock_label,
    ):
        process_report("issues-42", report)

    mock_label.assert_called_once_with(42)


def test_evil_discussion_is_not_labeled_after_confirmed_judgement():
    """
    Phase 2: Confirmed evil discussions should not receive issue labels.
    """
    from issue_auth_tool.issues_auth_tool import process_report

    report = ValidReport(
        type="evil",
        reason="malicious content detected",
        mcp=["view 1234"],
        source="discussions",
    )

    with (
        patch(
            "issue_auth_tool.issues_auth_tool.get_llm_response",
            return_value='["del 1"]',
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.handle_instruction",
            return_value="fake mcp context result",
        ),
        patch("issue_auth_tool.issues_auth_tool._execute_final_command"),
        patch("issue_auth_tool.issues_auth_tool.label_evil_issue") as mock_label,
    ):
        process_report("discussions-42", report)

    mock_label.assert_not_called()


def test_label_issue_uses_configured_label_key():
    """
    Phase 2: issue labels should be reusable through arbitrary config keys.
    """
    from issue_auth_tool import issues_auth_tool

    issue = MagicMock()
    issue.labels = []
    fake_repo = MagicMock()
    fake_repo.get_issue.return_value = issue

    with (
        patch.object(issues_auth_tool, "repo", fake_repo),
        patch.object(
            issues_auth_tool,
            "setting",
            {**issues_auth_tool.setting, "labels": {"triage": "needs-triage"}},
        ),
    ):
        issues_auth_tool.label_issue(42, "triage")

    issue.add_to_labels.assert_called_once_with("needs-triage")


# ──────────────────────────────────────────────────────────────
# Phase 3: Command execution & generate flow
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "final_decision_cmd,helper_method",
    [
        ("del 1", "do_del"),
        ("outdate 北京大学", "do_outdate"),
        ("alias 旧大学 新大学 1", "do_alias"),
    ],
)
def test_command_execution(final_decision_cmd, helper_method):
    """
    Phase 3: Final decision commands are correctly routed to the helper.

    - Call _execute_final_command directly with each command type.
    - Mock the helper's do_del/do_outdate/do_alias methods.
    - Verify the correct method is called with the expected arguments
      (including the auto-appended issue_id).
    """
    from issue_auth_tool.issues_auth_tool import _execute_final_command
    from issue_auth_tool.mcp import viewer as viewer_mod

    helper_mock = viewer_mod.helper
    with patch.object(helper_mock, helper_method) as mock_method:
        _execute_final_command(final_decision_cmd, 42)
        mock_method.assert_called_once()
        # The issue_id (42) should be appended to the command
        call_args = mock_method.call_args[0][0]
        assert "42" in call_args


def test_dry_run_skips_final_command_mutations():
    """
    Phase 3: dry-run logs commands without mutating viewer data.
    """
    from issue_auth_tool import issues_auth_tool
    from issue_auth_tool.mcp.viewer import helper as viewer_helper

    with patch.object(
        issues_auth_tool, "setting", {**issues_auth_tool.setting, "dry_run": True}
    ):
        with patch.object(viewer_helper, "do_del") as mock_del:
            issues_auth_tool._execute_final_command("del 1", 42)
        with patch.object(viewer_helper, "do_outdate") as mock_outdate:
            issues_auth_tool._execute_final_command("outdate 北京大学", 42)
        with patch.object(viewer_helper, "do_alias") as mock_alias:
            issues_auth_tool._execute_final_command("alias 旧大学 新大学", 42)

    mock_del.assert_not_called()
    mock_outdate.assert_not_called()
    mock_alias.assert_not_called()


def test_generate_called_at_end_of_run():
    """
    Phase 3: helper.do_generate() is called at the end of run()
    when there are valid reports.

    - Pre-populate all_valid_reports with one report.
    - Mock fetch_issues_and_discussions to return nothing (all pre-existing).
    - Mock get_llm_response for the judgement phase.
    - Only mock helper side effects (do_del, do_generate).
    - Verify do_generate is called exactly once.
    """
    from issue_auth_tool.issues_auth_tool import all_valid_reports, run
    from issue_auth_tool.mcp.viewer import helper as viewer_helper
    from issue_auth_tool.types import ValidReport

    all_valid_reports.clear()
    all_valid_reports["issues-42"] = ValidReport(
        type="evil",
        reason="test",
        mcp=["view 1"],
        source="issues",
    )

    fake_db_path = MagicMock()
    fake_db_path.exists.return_value = True

    with (
        patch(
            "issue_auth_tool.issues_auth_tool.fetch_issues_and_discussions",
            return_value=iter([]),
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.get_llm_response",
            return_value='["del 1"]',
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.handle_instruction",
            return_value="context",
        ),
        patch(
            "issue_auth_tool.issues_auth_tool._execute_final_command",
        ),
        patch.object(viewer_helper, "do_generate") as mock_generate,
        patch(
            "issue_auth_tool.issues_auth_tool.db_path",
            fake_db_path,
        ),
        patch(
            "issue_auth_tool.issues_auth_tool.setting",
            {
                "type": ["discussions"],
                "rate_per_minute": 10,
                "workers": 1,
                "prompt_type": "",
                "prompt_judgement": "",
                "google_query": "",
                "mcp": {"google": {"cx": "", "key": ""}, "viewer": {"config": ""}},
                "dry_run": False,
                "labels": {"evil": "evil-data"},
            },
        ),
    ):
        run()

    mock_generate.assert_called_once()


def test_unknown_command_logged_as_warning():
    """
    Phase 3: An unknown command in the final decision should log a warning.
    """
    from issue_auth_tool.issues_auth_tool import _execute_final_command

    with patch("issue_auth_tool.issues_auth_tool.logger") as mock_logger:
        _execute_final_command("foobar arg1", 42)
        mock_logger.warning.assert_called_once()
