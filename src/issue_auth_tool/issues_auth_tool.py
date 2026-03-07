import re
import shlex
from contextlib import suppress
from json import dumps, loads
from json.decoder import JSONDecodeError
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Callable, Iterable, Iterator, Never

from github import Auth, Github
from jsonschema import ValidationError
from openai import OpenAI
from rich.prompt import Prompt

from issue_auth_tool.types import PostData

from . import console_lock, logger
from .settings import config
from .utils.util import SCHEMA, edit_json, rate_limit, validate

g = Github(auth=Auth.Token(config['secret']['GITHUB_TOKEN']))
repo = g.get_repo(f'{config["secret"]["OWNER"]}/{config["secret"]["REPO_NAME"]}')
logger.debug(
    '已加载配置: owner=%s repo=%s llm_model=%s',
    config['secret']['OWNER'],
    config['secret']['REPO_NAME'],
    config['secret']['llm']['model'],
)
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

    producers: list[tuple[str, Callable[[], Iterator[PostData]]]] = []
    if 'issues' in types:
        producers.append(('issues', iter_issues))
    if 'discussions' in types:
        producers.append(('discussions', iter_discussions))
    logger.debug(
        '抓取配置: types=%s fetch_worker(s)=%s',
        types,
        [_[0] for _ in producers],
    )
    if not producers:
        return

    done = None
    post_queue: Queue[PostData | BaseException | None] = Queue()

    def produce_posts(source: str, iterator_factory: Callable[[], Iterator[PostData]]):
        logger.debug('抓取线程启动: %s', source)
        try:
            for post in iterator_factory():
                logger.debug('抓取到 %s #%s，准备入队', source, post['num'])
                post_queue.put(post)
        except BaseException as exc:
            logger.error('抓取 %s 失败: %s', source, exc)
            post_queue.put(exc)
        finally:
            logger.debug('抓取线程结束: %s', source)
            post_queue.put(done)

    workers = [
        Thread(
            target=produce_posts, args=(source, producer), name=f'fetch-{source}-{idx}'
        )
        for idx, (source, producer) in enumerate(producers, start=1)
    ]
    for worker in workers:
        worker.start()

    completed = 0
    errors: list[BaseException] = []
    try:
        while completed < len(workers):
            item = post_queue.get()
            if item is done:
                completed += 1
                continue
            if isinstance(item, BaseException):
                errors.append(item)
                continue
            logger.debug('消费队列内容: #%s', item['num'])
            yield item
    finally:
        for worker in workers:
            worker.join()

    if errors:
        raise errors[0]


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
    logger.debug('发送 LLM 请求: input_len=%s', len(input))
    ret = (
        client.chat.completions
        .create(
            model=config['secret']['llm']['model'],
            messages=[
                {'role': 'system', 'content': instructions},
                {'role': 'user', 'content': input},
            ],
        )
        .choices[0]
        .message.content
    )
    if ret is not None:
        logger.debug('收到 LLM 响应: output_len=%s', len(ret))
        return ret
    else:
        raise ValueError


db_path = Path(__file__).parent.parent / 'database'
def process_post(post: PostData) -> None:
    ret_text: str | None = None
    corrected: dict | None = None

    try:
        logger.debug('开始处理帖子: #%s %s', post['num'], post['title'])
        ret_text = get_llm_response(setting['prompt_type'], CONTENT.format(**post))
        logger.debug('LLM 原始输出: #%s %s', post['num'], ret_text)
        result: dict = loads(ret_text)
        validate(instance=result, schema=SCHEMA['type'])
        logger.debug('LLM 输出校验通过: #%s type=%s', post['num'], result.get('type'))
        result |= {'num': post['num']}
    except (JSONDecodeError, ValidationError) as e:
        with console_lock:
            answer = Prompt.ask(
                '解析出错，是否人工修改？',
                default='n',
                choices=['n', 'y'],
                case_sensitive=False,
            )
            if answer == 'y':
                editable = (
                    ret_text
                    if isinstance(e, JSONDecodeError)
                    else dumps(
                        loads(ret_text) if ret_text is not None else {},
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                corrected = edit_json(editable, SCHEMA['type'])  # type: ignore
                if corrected is not None:
                    corrected |= {'num': post['num']}
                    logger.info('修正后的数据： %s', corrected)
                else:
                    logger.error(
                        '编号 %s 验证失败: %s',
                        post['num'],
                        getattr(e, 'message', str(e)),
                    )
                    return
            else:
                logger.error(
                    '编号 %s 验证失败且未人工修正: %s',
                    post['num'],
                    getattr(e, 'message', str(e)),
                )
                return

    output = corrected if corrected is not None else result
    with suppress(FileExistsError):
        with open(
            db_path / f'{post["num"]}.json',
            'x',
            encoding='utf-8',
        ) as f:
            f.write(dumps(output, ensure_ascii=False, indent=2))
            logger.info('已保存编号 %s 的结果。', post['num'])


def run():
    worker_count = setting.get('workers', 4)
    logger.debug('帖子处理 worker_count=%s', worker_count)

    if not db_path.exists():
        db_path.mkdir(parents=True)

    post_queue: Queue[PostData | None] = Queue()
    error_queue: Queue[BaseException] = Queue()

    def consume_posts() -> None:
        while True:
            item = post_queue.get()
            if item is None:
                return
            try:
                process_post(item)
            except BaseException as exc:
                logger.error('处理帖子失败: %s', exc)
                error_queue.put(exc)

    workers = [
        Thread(target=consume_posts, name=f'process-post-{idx}')
        for idx in range(1, worker_count + 1)
    ]
    for worker in workers:
        worker.start()

    try:
        for post in fetch_issues_and_discussions(
            (int(p.stem) for p in db_path.glob('*.json'))
        ):
            post_queue.put(post)
    finally:
        for _ in workers:
            post_queue.put(None)
        for worker in workers:
            worker.join()

    if not error_queue.empty():
        raise error_queue.get()


if __name__ == '__main__':
    run()
