import logging

from rich.logging import RichHandler


def setup_logger() -> logging.Logger:
    logger = logging.getLogger('IAT')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    ch = RichHandler(
        rich_tracebacks=True, markup=True, show_time=False, show_path=False
    )
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(ch)
    return logger


logger = setup_logger()
