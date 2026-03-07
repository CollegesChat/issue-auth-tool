import logging
from threading import RLock

from rich.logging import RichHandler

HAS_OBJPRINT = False
try:
    from objprint import op
    HAS_OBJPRINT = True
except ImportError:
    pass

console_lock = RLock()


class LockedRichHandler(RichHandler):
    def emit(self, record):
        with console_lock:
            super().emit(record)

class InspectLoggerAdapter(logging.LoggerAdapter):
    def debug(self, msg, *args, obj=None, **kwargs):
        super().debug(msg, *args, **kwargs)

        if obj is not None and HAS_OBJPRINT:
            with console_lock:
                op(obj)


def setup_logger() -> logging.LoggerAdapter:
    logger = logging.getLogger('IAT')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    ch = LockedRichHandler(
        rich_tracebacks=True, markup=True, show_time=False, show_path=False
    )
    ch.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(ch)

    return InspectLoggerAdapter(logger)


logger = setup_logger()
