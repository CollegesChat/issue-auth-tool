from json import dumps

import pytest

from issue_auth_tool.utils import SCHEMA, edit_json

if __name__ != '__main__':
    pytest.skip('skipping this module', allow_module_level=True)  # type: ignore

pytestmark = pytest.mark.manual

data = {
    'type': 'alias',
    'reason': 'rule: found old/new name pattern',
    'mcp': ['view28272', 'google 西安电子科技大学西北电讯工程学院'],
}
edit_json(dumps(data), SCHEMA['type'])
data = {
    'type': 'invalid',
    'reason': 'rule: found old/new name pattern',
    'mcp': [''],
}
edit_json(dumps(data), SCHEMA['type'])