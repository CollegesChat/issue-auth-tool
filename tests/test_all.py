# import atexit
import json
from pathlib import Path
from typing import Literal

import pytest
from jsonschema import ValidationError

from issue_auth_tool.utils.util import validate as schema_validate

# atexit.register(save_on_exit)

# 项目根目录
SRC_DIR = Path(__file__).parent.parent
SCHEMA_DIR = SRC_DIR / 'src/issue_auth_tool/schema'
TEST_DIR = SRC_DIR / 'tests'

database: dict[int, Literal['invalid', 'processed']] = {}

# def save_on_exit():
#     with open('data.json', 'w', encoding='utf-8') as f:
#         json.dump(data, f, ensure_ascii=False, indent=4)

def load_json(filepath):
    """加载 JSON 文件"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_test_cases():
    cases = []

    # 遍历所有 schema 文件
    for schema_file in SCHEMA_DIR.glob('*.schema.json'):
        schema_name = schema_file.stem.replace('.schema', '')
        test_file = TEST_DIR / 'test_examples' / f'{schema_name}.json'

        if not test_file.exists():
            continue

        # 加载测试用例
        test_data = load_json(test_file)
        schema_data = load_json(schema_file)

        # 处理 legal 测试用例（预期通过）
        for idx, case in enumerate(test_data.get('legal', [])):
            cases.append(
                (
                    f'{schema_name}_legal_{idx}',
                    schema_data,
                    case,
                    True,  # should_pass
                )
            )

        # 处理 illegal 测试用例（预期失败）
        for idx, case in enumerate(test_data.get('illegal', [])):
            cases.append(
                (
                    f'{schema_name}_illegal_{idx}',
                    schema_data,
                    case,
                    False,  # should_pass
                )
            )
    return cases


@pytest.mark.parametrize('test_name,schema,data,should_pass', get_test_cases())
def test_json_schema_validation(test_name, schema, data, should_pass):
    """
    参数化测试：验证 JSON 数据是否符合 schema

    Args:
        test_name: 测试用例名称
        schema: JSON Schema 定义
        data: 待验证的数据
        should_pass: 是否预期验证通过
    """
    if should_pass:
        # legal 用例：应该验证通过
        try:
            schema_validate(instance=data, schema=schema)
        except ValidationError as e:
            raise AssertionError(f"Legal case '{test_name}' failed validation: {e.message}") from e

    else:
        # illegal 用例：应该验证失败
        with pytest.raises(ValidationError):
            schema_validate(instance=data, schema=schema)


pytest.main([__file__])
