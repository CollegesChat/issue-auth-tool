import pytest

from issue_auth_tool import logger

if __name__ != '__main__':
    pytest.skip('skipping this module', allow_module_level=True) # type: ignore


class Position:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class Player:
    def __init__(self):
        self.name = 'Alice'
        self.age = 18
        self.items = ['axe', 'armor']
        self.coins = {'gold': 1, 'silver': 33, 'bronze': 57}
        self.position = Position(3, 5)


# 现在颜色应该完美显示了
print(
    repr(
        Position(
            1,
            2,
        )
    )
)
logger.debug(
    'Checking GitHub Default Retry',
    obj=[Position(
        1,
        2,
    ),Player()]
)
print(repr(Position(
        1,
        2,
    )),repr(Player()))