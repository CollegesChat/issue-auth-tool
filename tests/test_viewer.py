import importlib
import sys


def test_viewer_loads_real_helper_and_views_data(tmp_path, monkeypatch):
    from issue_auth_tool import settings

    csv_path = tmp_path / "results_desensitized.csv"
    alias_path = tmp_path / "alias.txt"
    q_columns = [f"Q{i}" for i in range(5, 30)]
    csv_path.write_text(
        ",".join(["答题序号", *q_columns])
        + "\n"
        + ",".join(["123", "回答内容", *([""] * (len(q_columns) - 1))])
        + "\n",
        encoding="utf-8",
    )
    alias_path.write_text("旧大学🚮新大学\n", encoding="utf-8")

    viewer_module_name = "issue_auth_tool.mcp.viewer"
    original_viewer = sys.modules.pop(viewer_module_name, None)
    original_uniinfo_editor = sys.modules.pop("uniinfo_editor", None)
    viewer_config = f"{csv_path} {alias_path}"
    monkeypatch.setattr(
        settings,
        "setting",
        {
            **settings.setting,
            "mcp": {
                **settings.setting["mcp"],
                "viewer": {"config": viewer_config},
            },
        },
    )

    try:
        viewer = importlib.import_module(viewer_module_name)

        assert viewer.view("123").startswith("123\nQ5: 回答内容")
        assert viewer.view("missing") is None
    finally:
        sys.modules.pop(viewer_module_name, None)
        if original_viewer is not None:
            sys.modules[viewer_module_name] = original_viewer
        sys.modules.pop("uniinfo_editor", None)
        if original_uniinfo_editor is not None:
            sys.modules["uniinfo_editor"] = original_uniinfo_editor
