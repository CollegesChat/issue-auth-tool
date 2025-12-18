import re
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from json import dumps, loads
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import Iterable, Iterator

from github import Auth, Github
from jsonschema import ValidationError
from openai import OpenAI
from rich.prompt import Prompt

from settings import config

from .utils import SCHEMA, edit_json, logger, rate_limit, validate

type DecodeError = JSONDecodeError | ValidationError


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


def fetch_issues_and_discussions(
    ignore_nums: Iterable[int] = (),
) -> Iterator[dict]:
    types = setting['type']
    ignore: set[int] = set(ignore_nums)
    logger.warning(f'忽略以下编号： {ignore}')
    strip = strip_markdown
    repo_issues = repo.get_issues
    repo_discussions = repo.get_discussions
    def iter_issues():
        for issue in repo_issues(state='open'):
            # 过滤 PR
            if getattr(issue, 'pull_request', None):
                continue
            if issue.number in ignore:
                continue
            yield {
                'title': issue.title,
                'num': issue.number,
                'text': strip(issue.body or '(无内容)'),
            }

    def iter_discussions():
        for disc in repo_discussions(
            discussion_graphql_schema='id number title body',
            answered=False,
        ):
            if disc.number in ignore:
                continue
            yield {
                'title': disc.title,
                'num': disc.number,
                'text': strip(disc.body or '(无内容)')[:1024],
            }

    # 没有需要拉取的类型，直接返回
    tasks = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        if 'issues' in types:
            tasks.append(ex.submit(lambda: list(iter_issues())))
        if 'discussions' in types:
            tasks.append(ex.submit(lambda: list(iter_discussions())))

        for fut in as_completed(tasks):
            # 逐个任务完成后再逐条 yield，避免阻塞
            for item in fut.result():
                yield item


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


@rate_limit(setting['rate_per_minute'], 60)
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


db_path = Path(__file__).parent.parent / 'database'


def run():
    for post in fetch_issues_and_discussions(
        (int(p.stem) for p in db_path.glob('*.json'))
    ):
        ret = None
        try:
            ret = loads(
                get_llm_response(setting['prompt_type'], CONTENT.format(**post))
            )
            validate(instance=ret, schema=SCHEMA['type'])
            ret |= {'num': post['num']}
        except DecodeError as e:
            answer = Prompt.ask(
                '解析出错，是否人工修改？',
                default='n',
                choices=['n', 'y'],
                case_sensitive=False,
            )
            if answer == 'y':
                corrected = edit_json(ret, SCHEMA['type'])
                if corrected is not None:
                    corrected |= {'num': post['num']}
                    logger.info('修正后的数据： %s', corrected)
                else:
                    logger.error(f'编号 {post["num"]} 验证失败: {e.message}')
                    continue
        if not db_path.exists():
            db_path.mkdir(parents=True)
        with suppress(FileExistsError):
            with open(
                db_path / f'{post["num"]}.json',
                'x',
                encoding='utf-8',
            ) as f:
                if ret is not None:
                    f.write(dumps((ret or corrected), ensure_ascii=False, indent=2))
                    logger.info(f'已保存编号 {post["num"]} 的结果。')


if __name__ == '__main__':
    run()
