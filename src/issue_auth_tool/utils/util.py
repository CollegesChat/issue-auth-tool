import json
import threading
import time
from bisect import bisect_right, insort
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for
from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.widgets import Label, TextArea
from pygments.lexers.data import JsonLexer
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT7

from .. import logger


def load_regex(uri: str):
    # regex://username.regex → username.regex
    key = uri.replace('regex://', '')
    path = Path(__file__).parent / 'schema' / 'regex' / f'{key}'
    with open(path, 'r', encoding='utf-8') as f:
        return {'type': 'string', 'pattern': f.read().strip()}


def retrieve_schema(uri: str) -> Resource:
    if uri.startswith('regex://'):
        return Resource.from_contents(
            load_regex(uri), default_specification=DRAFT7
        )
    raise NoSuchResource(ref=uri)


registry = Registry(retrieve=retrieve_schema)


def formatter(e: ValidationError, instance: dict) -> str:
    return f"""JSON: {instance}
Message: {e.message}
Path: {list(e.path)}
Schema path: {list(e.schema_path)}
Validator: {e.validator}
Validator value: {e.validator_value}
Instance: {e.instance}"""


def validate(instance: object, schema: Any):
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema=schema, registry=registry)
    validator.validate(instance)


SCHEMA: dict[str, dict] = {
    'judgement': json.loads(
        (Path(__file__).parent / 'schema' / 'judgement.schema.json').read_text()
    ),
    'type': json.loads(
        (Path(__file__).parent / 'schema' / 'type.schema.json').read_text()
    ),
}


def edit_json(json_data: str, validator: dict) -> None | dict:
    """
    打开一个可编辑的 JSON 文本框，初始内容为 json_data 的漂亮打印。
    - 按 Ctrl-S 保存并退出：返回解析后的 dict（如果 JSON 有误，会打印错误并返回 None）。
    - 按 Esc 或 Ctrl-Q 取消编辑并返回 None。
    """
    text_area = TextArea(
        text=json_data,
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
    on_text_changed(SimpleNamespace(text=json_data))
    root_container = HSplit([label, text_area, status_label], padding=0)
    app = Application(
        layout=Layout(root_container),
        key_bindings=kb,
        full_screen=False,
        mouse_support=True,
        erase_when_done=True,
    )
    edited: str | None = app.run()

    if edited is None:
        return None

    try:
        parsed = json.loads(edited)
        validate(instance=parsed, schema=validator)
        # console.print(Syntax(json.dumps(parsed, indent=2, ensure_ascii=False),'json',theme='monokai',))
        return parsed
    except ValidationError as e:
        logger.error(formatter(e, edited))
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

    # 使用“预约执行时间”的方式做滑动窗口限速：
    # 锁内只负责计算当前调用最早可执行的时间，实际等待在锁外完成。
    reservations: list[float] = []
    lock = threading.Lock()

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with lock:
                now = time.monotonic()
                while reservations and reservations[0] <= now - per_seconds:
                    reservations.pop(0)

                active_left = bisect_right(reservations, now - per_seconds)
                active_right = bisect_right(reservations, now)
                active_now = active_right - active_left
                available_now = max(0, max_calls - active_now)

                scheduled_at = now
                while True:
                    left = bisect_right(reservations, scheduled_at - per_seconds)
                    right = bisect_right(reservations, scheduled_at)
                    if right - left < max_calls:
                        break
                    scheduled_at = reservations[left] + per_seconds

                insort(reservations, scheduled_at)
                reservation_count = len(reservations)

            logger.debug(
                'rate_limit[%s]: available_now=%s/%s scheduled_in=%.3fs reservations=%s',
                func.__name__,
                available_now,
                max_calls,
                max(0.0, scheduled_at - now),
                reservation_count,
            )

            sleep_for = scheduled_at - now
            if sleep_for > 0:
                time.sleep(sleep_for)

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
