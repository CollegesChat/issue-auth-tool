import re
import shlex
import threading
import time
from collections import deque
from functools import wraps
from json import loads
from typing import Iterator

from github import Auth, Github
from jsonschema import ValidationError, validate
from openai import OpenAI
from rich.prompt import Prompt

from settings import config

from . import logger
from .exceptions import DecodeError
from .utils import SCHEMA, edit_json, formatter

g = Github(auth=Auth.Token(config['secret']['GITHUB_TOKEN']))
repo = g.get_repo(f'{config["secret"]["OWNER"]}/{config["secret"]["REPO_NAME"]}')
print(config['secret'])
client = OpenAI(
    api_key=config['secret']['llm']['key'], base_url=config['secret']['llm']['server']
)
setting = config['settings']


def strip_markdown(md: str) -> str:
    text = md
    # 删除代码块 ```...``` 和 ~~~...~~~
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'~~~[\s\S]*?~~~', '', text)
    # 删除行内代码 `...`
    text = re.sub(r'`[^`]*`', '', text)
    # 删除图片 ![alt](url)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    # 删除链接 [text](url) -> 保留 text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # 删除加粗/斜体 **text**, __text__, *text*, _text_
    text = re.sub(r'(\*\*|__)(.*?)\1', r'\2', text)
    text = re.sub(r'(\*|_)(.*?)\1', r'\2', text)
    # 删除标题 #
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    # 删除引用 >
    text = re.sub(r'^>\s?', '', text, flags=re.MULTILINE)
    # 删除水平线 ---
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    # 删除多余的空白行
    text = re.sub(r'\n\s*\n', '\n', text)
    # 去除首尾空白
    return text.strip()


def fetch_issues_and_discussions() -> Iterator[dict]:
    # issues
    if 'issues' in setting['type']:
        for issue in repo.get_issues(state='open'):
            # 过滤 PR（在 PyGithub 中 pull_request 属性可能存在）
            if getattr(issue, 'pull_request', None):
                continue
            yield {
                'title': issue.title,
                'num': issue.number,
                'text': strip_markdown(issue.body or '(无内容)'),
            }

    # discussions
    if 'discussions' in setting['type']:
        for disc in repo.get_discussions(
            discussion_graphql_schema='id number title body', answered=False
        ):
            yield {
                'title': disc.title,
                'num': disc.number,
                'text': strip_markdown(disc.body or '(无内容)')[:1024],  # 限制长度
            }


def handle_instruction(instructions: list[str]) -> str:
    for instr in instructions:
        instr = shlex.split(instr)
        match instr[0]:
            case 'google':
                pass
            case 'view':
                pass


CONTENT = """
标题：{title}
编号：{num}
内容：{text}
"""


def rate_limit(max_calls: int, per_seconds: float):
    """
    通用限速装饰器：
    - max_calls：时间窗口内最多调用次数
    - per_seconds：窗口秒数
    """
    calls = deque()
    lock = threading.Lock()

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
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
                    # 清理已过期
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


@rate_limit(10, 60)
def get_llm_response(instructions: str, input: str) -> str:
    return (
        client.chat.completions.create(
            model=config['secret']['llm']['model'],
            messages=[
                {'role': 'system', 'content': instructions},
                {'role': 'user', 'content': input},
            ],
        )
        .choices[0]
        .message.content
    )


def run():
    for post in fetch_issues_and_discussions():
        ret = loads(get_llm_response(setting['prompt_type'], CONTENT.format(**post)))
        print(ret | {'num': post['num']})
        try:
            validate(instance=ret, schema=SCHEMA['type'])
            ret |= {'num': post['num']}
        except DecodeError as e:
            if {'y': False, 'n': True}[
                Prompt.ask(
                    '解析出错，是否人工修改？',
                    default='n',
                    choices=['n', 'y'],
                    case_sensitive=False,
                )
            ]:
                corrected = edit_json(ret, SCHEMA['type'])
                if corrected is not None:
                    corrected |= {'num': post['num']}
                    print('修正后的数据：', corrected)
                else:
                    print(f'编号 {post["num"]} 验证失败: {e.message}')
            else:
                pass


if __name__ == '__main__':
    # run()
    try:
        instance = {
            'type': 'alias',
            'reason': 'rule: found old/new name pattern',
            'mcp': [],
        }
        validate(
            instance={
                'type': 'alias',
                'reason': 'rule: found old/new name pattern',
                'mcp': [],
            },
            schema=SCHEMA['type'],
        )
    except ValidationError as e:
        logger.error(formatter(e, instance))
