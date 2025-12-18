import json
import sqlite3
import threading
import time
from collections import deque
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from jsonschema import RefResolver
from jsonschema import validate as _validate
from jsonschema.exceptions import ValidationError
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.widgets import Label, TextArea
from pygments.lexers.data import JsonLexer

from . import logger


def load_regex(uri: str):
    # regex://username.regex → username.regex
    key = uri.replace('regex://', '')
    path = Path(__file__).parent / 'schema' / 'regex' / f'{key}'
    with open(path, 'r', encoding='utf-8') as f:
        return {'type': 'string', 'pattern': f.read().strip()}


resolver = RefResolver(
    base_uri='',
    referrer=None,
    handlers={
        'regex': load_regex  # 注册 URI scheme loader
    },
)


def formatter(e: ValidationError, instance: dict) -> str:
    return f"""
JSON: {instance}
Message: {e.message}
Path: {list(e.path)}
Schema path: {list(e.schema_path)}
Validator: {e.validator}
Validator value: {e.validator_value}
Instance: {e.instance}
""".lstrip()


def validate(instance: object, schema: Any):
    return _validate(instance=instance, schema=schema, resolver=resolver)


SCHEMA: dict[str, object] = {
    'judgement': json.loads(
        (Path(__file__).parent / 'schema' / 'judgement.schema.json').read_text()
    ),
    'type': json.loads(
        (Path(__file__).parent / 'schema' / 'type.schema.json').read_text()
    ),
}


def edit_json(d: dict, validator: dict) -> None | dict:
    """
    打开一个可编辑的 JSON 文本框，初始内容为 j 的漂亮打印。
    - 按 Ctrl-S 保存并退出：返回解析后的 dict（如果 JSON 有误，会打印错误并返回 None）。
    - 按 Esc 或 Ctrl-Q 取消编辑并返回 None。
    """
    initial_json = json.dumps(d, ensure_ascii=False, indent=2)
    text_area = TextArea(
        text=initial_json,
        lexer=PygmentsLexer(JsonLexer),
        scrollbar=False,
        line_numbers=True,
        height=Dimension(weight=1),  # <-- 关键：可伸缩
        wrap_lines=False,
    )

    kb = KeyBindings()

    @kb.add('c-s')  # Ctrl-S 保存并退出，返回文本
    def _(event):
        event.app.exit(result=text_area.text)

    def on_text_changed(buf):
        try:
            parsed = json.loads(buf.text)
            validate(instance=parsed, schema=validator)
            status_label.text = FormattedText([
                ('fg:ansigreen', '状态：'),
                ('fg:ansiwhite', 'JSON 语法正确'),
            ])
        except Exception as ex:
            # 显示错误（红色），只显示简短信息以免太长
            status_label.text = FormattedText([
                ('fg:ansired', '状态：'),
                ('fg:ansiwhite', f'JSON 错误: {repr(ex).replace(r"\\\\", "\\")}'),
            ])

    text_area.buffer.on_text_changed += on_text_changed

    @kb.add('escape')  # Esc 取消
    @kb.add('c-q')  # Ctrl-Q 取消
    def _(event):
        event.app.exit(result=None)

    label = Label('按 Ctrl-S 保存并退出\n按 Esc 或 Ctrl-Q 取消编辑')
    status_label = Label('')
    on_text_changed(SimpleNamespace(text=initial_json))
    root_container = HSplit([label, text_area, status_label], padding=0)
    app = Application(
        layout=Layout(root_container),
        key_bindings=kb,
        full_screen=False,
        mouse_support=True,
        erase_when_done=True,
    )
    edited = app.run()

    if edited is None:
        return None

    try:
        parsed = json.loads(edited)
        validate(instance=parsed, schema=validator)
        # console.print(Syntax(json.dumps(parsed, indent=2, ensure_ascii=False),'json',theme='monokai',))
        return parsed
    except ValidationError as e:
        logger.error(formatter(e))
        return None
    except json.JSONDecodeError as e:
        logger.error(f'[red]JSON 解析错误:[/red] {e}')
        return None


def rate_limit(max_calls: int, per_seconds: float):
    """
    通用限速装饰器：
    - max_calls：时间窗口内最多调用次数。如果 max_calls=0，则禁用限速。
    - per_seconds：窗口秒数
    """

    # max_calls=0 时，直接返回一个透传装饰器
    if max_calls <= 0:

        def bypass_decorator(func):
            return func

        return bypass_decorator

    # 正常限速逻辑
    calls = deque()
    lock = threading.Lock()

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 限速逻辑
            with lock:
                now = time.time()
                # 清理窗口外的调用记录
                while calls and now - calls[0] > per_seconds:
                    calls.popleft()

                # 如果超过限额，等待直到下一次可用
                if len(calls) >= max_calls:
                    sleep_for = per_seconds - (now - calls[0])
                    if sleep_for > 0:
                        time.sleep(sleep_for)

                    # 重新获取当前时间并再次清理已过期记录
                    now = time.time()
                    while calls and now - calls[0] > per_seconds:
                        calls.popleft()

                calls.append(time.time())

            # 调用目标函数，自动重试 429
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    msg = str(e)
                    # 简单识别 429 或 RESOURCE_EXHAUSTED
                    if '429' in msg or 'RESOURCE_EXHAUSTED' in msg:
                        time.sleep(1)
                        continue
                    raise

        return wrapper

    return decorator



class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def exec(self, sql: str, args=()):
        self.conn.execute(sql, args)
        self.conn.commit()

    def insert(self, table: str, row: Mapping):
        cols = ', '.join(row)
        qs = ', '.join('?' for _ in row)
        self.exec(
            f'INSERT INTO {table} ({cols}) VALUES ({qs})',
            tuple(row.values()),
        )

    def update_status(self, num: int, status: str):
        self.exec(
            'UPDATE task SET status = ? WHERE num = ?',
            (status, num),
        )

    def select(self, sql: str, args=()) -> list[dict]:
        cur = self.conn.execute(sql, args)
        return [dict(r) for r in cur]