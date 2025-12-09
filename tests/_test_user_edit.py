from src.utils import SCHEMA, edit_json

data = {
    'type': 'alias',
    'reason': 'rule: found old/new name pattern',
    'mcp': ['view28272', 'google 西安电子科技大学西北电讯工程学院'],
}
edit_json(data, SCHEMA['type'])
data = {
    'type': 'invalid',
    'reason': 'rule: found old/new name pattern',
    'mcp': [''],
}
edit_json(data, SCHEMA['type'])