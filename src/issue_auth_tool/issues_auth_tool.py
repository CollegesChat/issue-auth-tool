import re
import shlex
from contextlib import suppress
from json import dumps, loads
from json.decoder import JSONDecodeError
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Callable, Iterable, Iterator, cast

from github import Auth, Github
from jsonschema import ValidationError
from openai import OpenAI
from rich.prompt import Prompt

from issue_auth_tool.types import DeferredPost, PostData, ValidReport

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
all_valid_reports: dict[int, ValidReport] = {}

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
            discussion_graphql_schema='id number title body',  # TODO: 過濾指定id的討論
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
    from .mcp.google import get_results
    from .mcp.viewer import view

    results = []
    for instr in instructions:
        parts = shlex.split(instr)
        match parts[0]:
            case 'google':
                results.append(
                    get_results(
                        parts[1],
                        setting['mcp']['google']['key'],
                        setting['mcp']['google']['cx'],
                    )
                )
            case 'view':
                results.append(view(parts[1]))
            case _:
                raise ValueError(f'未知指令: {parts[0]}')
    return '\n\n---\n\n'.join(results)


CONTENT = """
标题：{title}
编号：{num}
内容：{text}
"""


@rate_limit(setting['rate_per_minute'], 60)
def get_llm_response(instructions: str, input: str) -> str:
    model = config['secret']['llm']['model']
    extra: dict = {}
    if model.startswith('qwen'):
        extra['extra_body'] = {'enable_thinking': False}
    ret = (
        client.chat.completions
        .create(
            model=model,
            messages=[
                {'role': 'system', 'content': instructions},
                {'role': 'user', 'content': input},
            ],
            **extra,
        )
        .choices[0]
        .message.content
    )
    if ret is not None:
        return ret
    else:
        raise ValueError


db_path = Path(__file__).parent.parent / 'database'


def build_editable_text(ret_text: str) -> str:

    try:
        return dumps(loads(ret_text), indent=2, ensure_ascii=False)
    except JSONDecodeError:
        return ret_text


def prompt_manual_fix(deferred: DeferredPost) -> dict | None:
    post = deferred.post
    answer = Prompt.ask(
        """解析出错，是否人工修改？
\tn = 不修改不保存结果
\ty = 修改后保存结果
\ti = 忽略并保存结果""",
        default='n',
        choices=['n', 'y', 'i'],
        case_sensitive=False,
    )
    if answer == 'n':
        logger.error(
            '编号 %s 解析失败且未人工修正。',
            post['num'],
        )
        return None
    elif answer == 'i':
        try:
            logger.warning(
                '编号 %s 解析失败且将保存。',
                post['num'],
            )
            return loads(deferred.ret_text)
        except JSONDecodeError:
            logger.error(
                '编号 %s JSON解析失败，无法报错。',
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
        ret_text: str = ''
        try:
            logger.debug('开始处理帖子: #%s %s', post['num'], post['title'])
            ret_text = get_llm_response(setting['prompt_type'], CONTENT.format(**post))

            logger.debug('LLM 原始输出: #%s %s', post['num'], ret_text)
            result: dict = cast(dict, loads(ret_text))
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

    save_post_output(post, output)  # type: ignore
    if output is not None and output['type'] != 'invalid':
        all_valid_reports[post['num']] = ValidReport(
            mcp=output['mcp'], reason=output['reason'], type=output['type']
        )


def _execute_final_command(cmd: str, issue_id: int) -> None:
    """解析 LLM 返回的最终指令并执行，自动追加 issue_id。"""
    from .mcp.viewer import helper

    parts = shlex.split(cmd)
    if not parts:
        return

    match parts[0]:
        case 'del':
            # del ID [issueId...]
            helper.do_del(f'{parts[1]} {issue_id}')
        case 'outdate':
            # outdate ID [issueId...]
            helper.do_outdate(f'{parts[1]} {issue_id}')
        case 'alias':
            # alias oldName newName [issueId...]
            helper.do_alias(f'{parts[1]} {parts[2]} {issue_id}')
        case _:
            logger.warning('未知的最终决策指令: %s', parts[0])


def process_report(num: int, report: ValidReport) -> None:
    # Step 3: 执行 MCP 指令获取上下文信息
    logger.info('开始处理报告 #%s: type=%s reason=%s mcp=%s',
                num, report['type'], report['reason'], report['mcp'])
    mcp_context = handle_instruction(report['mcp'])
    logger.debug('MCP 执行结果: %s', mcp_context)

    # Step 4: 合并信息，发送 LLM 做二次判定
    judgement_input = (
        setting['prompt_judgement'].format(
            type=report['type'],
            reasons=report['reason'],
        )
        + f'\n\nMCP 获取的补充信息：\n{mcp_context}'
    )
    ret_text = get_llm_response(judgement_input, '')

    # 解析并验证输出
    try:
        result = loads(ret_text)
        validate(instance=result, schema=SCHEMA['judgement'])
    except (JSONDecodeError, ValidationError) as e:
        logger.error(
            '二次判定输出无效 #%s (type=%s): %s\n原始输出: %s',
            num, report['type'], e, ret_text
        )
        return

    # Step 5: 执行最终决策或标记为无需处理
    if result is None:
        logger.info('二次判定结果: 无需处理 #%s (type=%s)', num, report['type'])
    else:
        for cmd in result:
            logger.info('最终决策 #%s: %s', num, cmd)
            _execute_final_command(cmd, num)


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

    # 处理所有有效报告的二次判定循环
    if all_valid_reports:
        logger.info('开始处理 %s 个有效报告的二次判定。', len(all_valid_reports))
        for num, report in all_valid_reports.items():
            try:
                process_report(num, report)
            except BaseException as exc:
                logger.error('处理报告失败: %s', exc)
                error_queue.append(exc)

    # 生成 changelog
    if all_valid_reports:
        from .mcp.viewer import helper
        helper.do_generate()

    logger.debug('可用報告：', obj=all_valid_reports)
    if error_queue:
        raise error_queue[0]


if __name__ == '__main__':
    run()
