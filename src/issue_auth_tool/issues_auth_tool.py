import re
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from json import dumps, loads
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import Iterable, Iterator, Never

from github import Auth, Github
from jsonschema import ValidationError
from openai import OpenAI
from rich.prompt import Prompt

from issue_auth_tool.types import PostData

from . import logger
from .settings import config
from .utils import SCHEMA, edit_json, rate_limit, validate

g = Github(auth=Auth.Token(config['secret']['GITHUB_TOKEN']))
repo = g.get_repo(f'{config["secret"]["OWNER"]}/{config["secret"]["REPO_NAME"]}')
print(config['secret'])
client = OpenAI(
    api_key=config['secret']['llm']['key'], base_url=config['secret']['llm']['server']
)
setting = config['settings']


MARKDOWN_CLEANER = re.compile(
        r"""
          (```[\s\S]*?```|~~~[\s\S]*?~~~)   #  匹配代碼塊 (Block Code)
        | (!\[.*?\]\(.*?\))                 #  匹配圖片 (Images)
        | (\[(?P<link_text>[^\]]+)\]\([^)]+\)) #  匹配鏈接 (Links)，捕獲組名為 link_text
        | (\*\*|__)(?P<bold_text>.*?)       #  匹配加粗 (Bold)
        | (\*|_)(?P<italic_text>.*?)        #  匹配斜體 (Italic)
        | (^\s*\#+\s*)                      #  匹配標題符號 (Headers)
        | (^\s*>\s?)                        #  匹配引用符號 (Blockquotes)
        | (^\s*-{3,}\s*$)                   #  匹配水平線 (HR)
    """,
        re.VERBOSE | re.MULTILINE,
    )
def strip_markdown(md):

    # 定義替換邏輯的函數
    def replace_func(match):
        # 提取鏈接文字、加粗文字或斜體文字
        if match.group('link_text'):
            return match.group('link_text')
        if match.group('bold_text'):
            return match.group('bold_text')
        if match.group('italic_text'):
            return match.group('italic_text')
        # 其他匹配項（如代碼塊、圖片、標題號）直接刪除
        return ''
    text = MARKDOWN_CLEANER.sub(replace_func, md)

    text = re.sub(r'\n\s*\n', '\n', text)
    return text.strip()


def fetch_issues_and_discussions(
    ignore_nums: Iterable[int] = (),
) -> Iterator[PostData]:
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
    return ''

CONTENT = """
标题：{title}
编号：{num}
内容：{text}
"""


@rate_limit(setting['rate_per_minute'], 60)
def get_llm_response(instructions: str, input: str) -> str | Never:
    ret= client.chat.completions.create(
            model=config['secret']['llm']['model'],
            messages=[
                {'role': 'system', 'content': instructions},
                {'role': 'user', 'content': input},
            ],
        ).choices[0].message.content
    if ret is not None:
        return ret
    else:
        raise ValueError



db_path = Path(__file__).parent.parent / 'database'


def run():
    for post in fetch_issues_and_discussions(
        (int(p.stem) for p in db_path.glob('*.json'))
    ):
        ret = None
        try:
            ret = get_llm_response(
                setting['prompt_type'], CONTENT.format(**post)
            )
            ret: dict = loads(ret)
            validate(instance=ret, schema=SCHEMA['type'])
            ret |= {'num': post['num']}
        except (JSONDecodeError,ValidationError) as e:
            answer = Prompt.ask(
                '解析出错，是否人工修改？',
                default='n',
                choices=['n', 'y'],
                case_sensitive=False,
            )
            if answer == 'y':
                corrected = edit_json(ret if e is JSONDecodeError else dumps(ret, indent=2,ensure_ascii=False), SCHEMA['type'])
                if corrected is not None:
                    corrected |= {'num': post['num']}
                    logger.info('修正后的数据： %s', corrected)
                else:
                    logger.error(f'编号 {post["num"]} 验证失败: {getattr(e,'message', str(e))}')
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
