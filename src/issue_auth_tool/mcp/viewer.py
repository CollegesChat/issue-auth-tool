from uniinfo_editor import UniInfoTUI

from ..issues_auth_tool import setting

helper = UniInfoTUI()
helper.do_load(setting['mcp']['viewer']['config'])


def view(answer: str):
    if answer not in helper.data:
        return None
    return '\n'.join(
        [
            answer,
            *[f'Q{i}: ' + helper.data[answer].get(f'Q{i}', '') for i in range(5, 30)],
        ]
    )
