import logging

from rich.logging import RichHandler

HAS_OBJPRINT = False
try:
    from objprint import op
    HAS_OBJPRINT = True
except ImportError:
    pass

class InspectLoggerAdapter(logging.LoggerAdapter):
    def debug(self, msg, *args, obj=None, **kwargs):
        super().debug(msg, *args, **kwargs)

        if obj is not None and HAS_OBJPRINT:
            op(obj)

def setup_logger()->logging.LoggerAdapter:
    logger = logging.getLogger('IAT')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    ch = RichHandler(
        rich_tracebacks=True, markup=True, show_time=False, show_path=False
    )
    ch.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(ch)

    return InspectLoggerAdapter(logger)


logger = setup_logger()
