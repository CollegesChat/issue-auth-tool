import re
import shlex
from contextlib import suppress
from dataclasses import dataclass
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

from . import logger
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
            discussion_graphql_schema='id number title body', # TODO: 過濾指定id的討論
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


@dataclass(slots=True)
class DeferredPost:
    post: PostData
    ret_text: str | None


def build_editable_text(ret_text: str | None) -> str:
    if ret_text is None:
        return ''

    try:
        return dumps(loads(ret_text), indent=2, ensure_ascii=False)
    except JSONDecodeError:
        return ret_text


def prompt_manual_fix(deferred: DeferredPost) -> dict | None:
    post = deferred.post
    answer = Prompt.ask(
        '解析出错，是否人工修改？',
        default='n',
        choices=['n', 'y'],
        case_sensitive=False,
    )
    if answer != 'y':
        logger.error(
            '编号 %s 解析失败且未人工修正。',
            post['num'],
        )
        return None

    editable = build_editable_text(deferred.ret_text)
    corrected = edit_json(editable, SCHEMA['type'])
    if corrected is None:
        logger.error('编号 %s 解析失败。', post['num'])
        return None

    corrected |= {'num': post['num']}
    logger.info('修正后的数据： %s', corrected)
    return corrected


def save_post_output(post: PostData, output: dict) -> None:
    with suppress(FileExistsError):
        with open(
            db_path / f'{post["num"]}.json',
            'x',
            encoding='utf-8',
        ) as f:
            f.write(dumps(output, ensure_ascii=False, indent=2))
            logger.info('已保存编号 %s 的结果。', post['num'])


def process_post(
    post: PostData,
    *,
    prompt_on_failure: bool = True,
    deferred_failure: DeferredPost | None = None,
) -> DeferredPost | None:
    output: dict | None = None
    failure = deferred_failure

    if failure is None:
        ret_text: str | None = None
        try:
            logger.debug('开始处理帖子: #%s %s', post['num'], post['title'])
            ret_text = get_llm_response(setting['prompt_type'], CONTENT.format(**post))
            logger.debug('LLM 原始输出: #%s %s', post['num'], ret_text)
            result: dict = loads(ret_text)
            validate(instance=result, schema=SCHEMA['type'])
            logger.debug(
                'LLM 输出校验通过: #%s type=%s', post['num'], result.get('type')
            )
            result |= {'num': post['num']}
            output = result
        except (JSONDecodeError, ValidationError):
            failure = DeferredPost(
                post=post,
                ret_text=ret_text,
            )
            if not prompt_on_failure:
                logger.warning('编号 %s 解析失败，已推迟到最后串行处理。', post['num'])
                return failure

    if failure is not None:
        output = prompt_manual_fix(failure)
        if output is None:
            return None

    save_post_output(post, output) # type: ignore
    return None


def run():
    worker_count = setting.get('workers', 4)
    logger.debug('帖子处理 worker_count=%s', worker_count)

    if not db_path.exists():
        db_path.mkdir(parents=True)

    post_queue: Queue[PostData | None] = Queue()
    error_queue: list[BaseException] = []
    deferred_posts: list[DeferredPost] = []

    def consume_posts() -> None:
        while True:
            item = post_queue.get()
            if item is None:
                return
            try:
                deferred = process_post(item, prompt_on_failure=False)
                if deferred is not None:
                    deferred_posts.append(deferred)
            except BaseException as exc:
                logger.error('处理帖子失败: %s', exc)
                error_queue.append(exc)

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

    if deferred_posts:
        logger.info('开始串行处理 %s 个解析失败任务。', len(deferred_posts))
        for deferred in deferred_posts:
            try:
                process_post(deferred.post, deferred_failure=deferred)
            except BaseException as exc:
                logger.error('串行处理帖子失败: %s', exc)
                error_queue.append(exc)

    if error_queue:
        raise error_queue[0]


if __name__ == '__main__':
    run()
