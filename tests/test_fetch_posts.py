import time
from importlib import import_module
from json import dumps, loads
from queue import Queue
from threading import Event, Lock, Thread
from types import SimpleNamespace


class FakeRepo:
    def get_issues(self, state='open'):
        time.sleep(0.05)
        yield SimpleNamespace(
            title='Issue 1',
            number=1,
            body='first issue',
            pull_request=None,
        )
        time.sleep(0.3)
        yield SimpleNamespace(
            title='Issue 2',
            number=2,
            body='second issue',
            pull_request=None,
        )

    def get_discussions(self, discussion_graphql_schema, answered):
        time.sleep(0.1)
        yield SimpleNamespace(
            title='Discussion 1',
            number=101,
            body='first discussion',
        )


def load_issues_auth_tool(monkeypatch, repo):
    import sys

    import github
    import openai

    class FakeGithubClient:
        def __init__(self, auth):
            self.auth = auth

        def get_repo(self, full_name):
            return repo

    monkeypatch.setattr(github.Auth, 'Token', lambda token: object())
    monkeypatch.setattr(github, 'Github', FakeGithubClient)
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: object())
    sys.modules.pop('issue_auth_tool.issues_auth_tool', None)

    issues_auth_tool = import_module('issue_auth_tool.issues_auth_tool')
    monkeypatch.setitem(issues_auth_tool.setting, 'type', ['issues', 'discussions'])
    monkeypatch.setattr(issues_auth_tool, 'repo', repo)
    return issues_auth_tool


def test_fetch_issues_and_discussions_streams_results(monkeypatch):
    issues_auth_tool = load_issues_auth_tool(monkeypatch, FakeRepo())

    posts = list(issues_auth_tool.fetch_issues_and_discussions())

    assert [post['num'] for post in posts] == [1, 101, 2]


def test_fetch_issues_and_discussions_runs_in_parallel(monkeypatch):
    class SlowRepo:
        def get_issues(self, state='open'):
            time.sleep(0.25)
            yield SimpleNamespace(
                title='Issue 1',
                number=1,
                body='first issue',
                pull_request=None,
            )

        def get_discussions(self, discussion_graphql_schema, answered):
            time.sleep(0.25)
            yield SimpleNamespace(
                title='Discussion 1',
                number=101,
                body='first discussion',
            )

    issues_auth_tool = load_issues_auth_tool(monkeypatch, SlowRepo())

    started_at = time.perf_counter()
    posts = list(issues_auth_tool.fetch_issues_and_discussions())
    elapsed = time.perf_counter() - started_at

    assert {post['num'] for post in posts} == {1, 101}
    assert elapsed < 0.3


def test_fetch_issues_and_discussions_starts_both_producers_before_yield(monkeypatch):
    class CoordinatedRepo:
        def __init__(self):
            self.issues_started = Event()
            self.discussions_started = Event()
            self.release = Event()

        def get_issues(self, state='open'):
            self.issues_started.set()
            assert self.discussions_started.wait(1)
            assert self.release.wait(1)
            yield SimpleNamespace(
                title='Issue 1',
                number=1,
                body='first issue',
                pull_request=None,
            )

        def get_discussions(self, discussion_graphql_schema, answered):
            self.discussions_started.set()
            assert self.issues_started.wait(1)
            assert self.release.wait(1)
            yield SimpleNamespace(
                title='Discussion 1',
                number=101,
                body='first discussion',
            )

    repo = CoordinatedRepo()
    issues_auth_tool = load_issues_auth_tool(monkeypatch, repo)
    posts = issues_auth_tool.fetch_issues_and_discussions()
    first_post_queue: Queue[object] = Queue()

    def consume_first_post():
        try:
            first_post_queue.put(next(posts))
        except BaseException as exc:
            first_post_queue.put(exc)

    consumer = Thread(target=consume_first_post)
    consumer.start()

    assert repo.issues_started.wait(1)
    assert repo.discussions_started.wait(1)
    assert first_post_queue.empty()

    repo.release.set()
    consumer.join(1)

    first_post = first_post_queue.get_nowait()
    assert not isinstance(first_post, BaseException)
    assert first_post['num'] in {1, 101} # type: ignore


def test_run_processes_posts_with_configured_workers(monkeypatch, tmp_path):
    issues_auth_tool = load_issues_auth_tool(monkeypatch, FakeRepo())
    posts = [
        {'title': 'Issue 1', 'num': 1, 'text': 'first issue'},
        {'title': 'Issue 2', 'num': 2, 'text': 'second issue'},
        {'title': 'Issue 3', 'num': 3, 'text': 'third issue'},
    ]

    monkeypatch.setitem(issues_auth_tool.setting, 'workers', 2)
    monkeypatch.setattr(issues_auth_tool, 'db_path', tmp_path)
    monkeypatch.setattr(
        issues_auth_tool,
        'fetch_issues_and_discussions',
        lambda ignore_nums=(): iter(posts),
    )

    active = 0
    max_active = 0
    active_lock = Lock()
    two_workers_started = Event()
    release_workers = Event()

    def fake_get_llm_response(instructions: str, input: str) -> str:
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                two_workers_started.set()
        if max_active < 2:
            assert two_workers_started.wait(1)
        assert release_workers.wait(1)
        with active_lock:
            active -= 1
        return '{"type":"invalid","reason":"ok","mcp":[]}'

    monkeypatch.setattr(issues_auth_tool, 'get_llm_response', fake_get_llm_response)

    run_error: list[BaseException] = []

    def invoke_run() -> None:
        try:
            issues_auth_tool.run()
        except BaseException as exc:
            run_error.append(exc)

    runner = Thread(target=invoke_run)
    runner.start()

    assert two_workers_started.wait(1)
    release_workers.set()
    runner.join(1)

    assert not runner.is_alive()
    assert not run_error
    assert max_active == 2
    assert sorted(path.stem for path in tmp_path.glob('*.json')) == ['1', '2', '3']


def test_process_post_validation_failure_triggers_manual_edit_fallback(
    monkeypatch, tmp_path
):
    issues_auth_tool = load_issues_auth_tool(monkeypatch, FakeRepo())
    post = {'title': 'Issue 1', 'num': 1, 'text': 'first issue'}
    invalid_llm_output = {'type': 'invalid', 'reason': 'ok', 'mcp': ['view 1']}
    corrected = {'type': 'invalid', 'reason': 'fixed', 'mcp': []}
    asked: list[str] = []
    edit_calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(issues_auth_tool, 'db_path', tmp_path)
    monkeypatch.setattr(
        issues_auth_tool,
        'get_llm_response',
        lambda instructions, input: dumps(invalid_llm_output, ensure_ascii=False),
    )
    monkeypatch.setattr(
        issues_auth_tool.Prompt,
        'ask',
        lambda *args, **kwargs: asked.append(args[0]) or 'y',
    )

    def fake_edit_json(editable: str, schema: dict) -> dict:
        edit_calls.append((editable, schema))
        return corrected.copy()

    monkeypatch.setattr(issues_auth_tool, 'edit_json', fake_edit_json)

    issues_auth_tool.process_post(post)

    assert asked == ['解析出错，是否人工修改？']
    assert len(edit_calls) == 1
    assert loads(edit_calls[0][0]) == invalid_llm_output
    assert edit_calls[0][1] == issues_auth_tool.SCHEMA['type']
    assert loads((tmp_path / '1.json').read_text(encoding='utf-8')) == {
        **corrected,
        'num': 1,
    }


def test_process_post_validation_failure_can_skip_manual_edit(
    monkeypatch, tmp_path
):
    issues_auth_tool = load_issues_auth_tool(monkeypatch, FakeRepo())
    post = {'title': 'Issue 1', 'num': 1, 'text': 'first issue'}

    monkeypatch.setattr(issues_auth_tool, 'db_path', tmp_path)
    monkeypatch.setattr(
        issues_auth_tool,
        'get_llm_response',
        lambda instructions, input: dumps(
            {'type': 'invalid', 'reason': 'ok', 'mcp': ['view 1']},
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(issues_auth_tool.Prompt, 'ask', lambda *args, **kwargs: 'n')

    edit_called = False

    def fake_edit_json(editable: str, schema: dict) -> dict:
        nonlocal edit_called
        edit_called = True
        return {'type': 'invalid', 'reason': 'fixed', 'mcp': []}

    monkeypatch.setattr(issues_auth_tool, 'edit_json', fake_edit_json)

    issues_auth_tool.process_post(post)

    assert not edit_called
    assert not (tmp_path / '1.json').exists()


def test_process_post_can_defer_validation_failure(monkeypatch, tmp_path):
    issues_auth_tool = load_issues_auth_tool(monkeypatch, FakeRepo())
    post = {'title': 'Issue 1', 'num': 1, 'text': 'first issue'}

    monkeypatch.setattr(issues_auth_tool, 'db_path', tmp_path)
    monkeypatch.setattr(
        issues_auth_tool,
        'get_llm_response',
        lambda instructions, input: dumps(
            {'type': 'invalid', 'reason': 'ok', 'mcp': ['view 1']},
            ensure_ascii=False,
        ),
    )
    prompt_called = False
    edit_called = False

    def fake_prompt(*args, **kwargs):
        nonlocal prompt_called
        prompt_called = True
        return 'n'

    def fake_edit_json(editable: str, schema: dict) -> dict:
        nonlocal edit_called
        edit_called = True
        return {'type': 'invalid', 'reason': 'fixed', 'mcp': []}

    monkeypatch.setattr(issues_auth_tool.Prompt, 'ask', fake_prompt)
    monkeypatch.setattr(issues_auth_tool, 'edit_json', fake_edit_json)

    deferred = issues_auth_tool.process_post(post, prompt_on_failure=False)

    assert deferred is not None
    assert deferred.post == post
    assert deferred.ret_text is not None
    assert loads(deferred.ret_text) == {
        'type': 'invalid',
        'reason': 'ok',
        'mcp': ['view 1'],
    }
    assert not prompt_called
    assert not edit_called
    assert not (tmp_path / '1.json').exists()


def test_run_defers_failed_posts_until_parallel_work_finishes(monkeypatch, tmp_path):
    issues_auth_tool = load_issues_auth_tool(monkeypatch, FakeRepo())
    posts = [
        {'title': 'Issue 1', 'num': 1, 'text': 'first issue'},
        {'title': 'Issue 2', 'num': 2, 'text': 'second issue'},
    ]
    prompt_called = Event()
    second_started = Event()
    release_second = Event()

    monkeypatch.setitem(issues_auth_tool.setting, 'workers', 2)
    monkeypatch.setattr(issues_auth_tool, 'db_path', tmp_path)
    monkeypatch.setattr(
        issues_auth_tool,
        'fetch_issues_and_discussions',
        lambda ignore_nums=(): iter(posts),
    )

    def fake_get_llm_response(instructions: str, input: str) -> str:
        if '编号：1' in input:
            return dumps(
                {'type': 'invalid', 'reason': 'ok', 'mcp': ['view 1']},
                ensure_ascii=False,
            )

        second_started.set()
        assert release_second.wait(1)
        return '{"type":"invalid","reason":"ok","mcp":[]}'

    monkeypatch.setattr(issues_auth_tool, 'get_llm_response', fake_get_llm_response)
    monkeypatch.setattr(
        issues_auth_tool.Prompt,
        'ask',
        lambda *args, **kwargs: prompt_called.set() or 'y',
    )
    monkeypatch.setattr(
        issues_auth_tool,
        'edit_json',
        lambda editable, schema: {'type': 'invalid', 'reason': 'fixed', 'mcp': []},
    )

    run_error: list[BaseException] = []

    def invoke_run() -> None:
        try:
            issues_auth_tool.run()
        except BaseException as exc:
            run_error.append(exc)

    runner = Thread(target=invoke_run)
    runner.start()

    assert second_started.wait(1)
    assert not prompt_called.wait(0.2)

    release_second.set()
    runner.join(1)

    assert not runner.is_alive()
    assert not run_error
    assert prompt_called.is_set()
    assert sorted(path.stem for path in tmp_path.glob('*.json')) == ['1', '2']
