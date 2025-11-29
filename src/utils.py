import json
from pathlib import Path

from jsonschema import validate
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


SCHEMA: dict[str, str] = {
    'judgement': json.loads(
        (Path(__file__).parent / 'schema' / 'judgement.schema.json').read_text()
    ),
    'type': json.loads(
        (Path(__file__).parent / 'schema' / 'type.schema.json').read_text()
    ),
}


def edit_json(j: dict, validator: dict) -> None | dict:
    """
    打开一个可编辑的 JSON 文本框，初始内容为 j 的漂亮打印。
    - 按 Ctrl-S 保存并退出：返回解析后的 dict（如果 JSON 有误，会打印错误并返回 None）。
    - 按 Esc 或 Ctrl-Q 取消编辑并返回 None。
    """
    initial_json = json.dumps(j, ensure_ascii=False, indent=2)
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
                ('fg:ansired', '状态：'),('fg:ansiwhite', f'JSON 错误: {ex!r}'),
            ])
        # 触发重绘（需要 app 实例；用闭包里存的 app）
        if app_is_running[0]:
            app.invalidate()

    # 需要一个小技巧让回调里能访问 app：用可变容器占位，稍后赋值
    app_is_running = [False]
    text_area.buffer.on_text_changed += on_text_changed
    @kb.add('escape')  # Esc 取消
    @kb.add('c-q')  # Ctrl-Q 取消
    def _(event):
        event.app.exit(result=None)

    label = Label('按 Ctrl-S 保存并退出\n按 Esc 或 Ctrl-Q 取消编辑')
    status_label = Label("")

    root_container = HSplit([label, text_area, status_label],padding=0)
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


logger.info(edit_json(
    {
        'type': 'alias',
        'reason': 'rule: found old/new name pattern',
        'mcp': [],
    },SCHEMA['type']
))
