import logging
import sys


def setup_logging(level: str = "INFO"):
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    # Avoid duplicate handlers on reload
    if not root.handlers:
        try:
            from rich.logging import RichHandler
            from rich.console import Console

            handler = RichHandler(rich_tracebacks=True, console=Console())
            fmt = "%(message)s"
            datefmt = "[%X]"
        except Exception:
            handler = logging.StreamHandler(sys.stdout)
            fmt = "%(asctime)s %(levelname)s %(name)s | %(message)s"
            datefmt = "%H:%M:%S"

        logging.basicConfig(level=lvl, format=fmt, datefmt=datefmt, handlers=[handler])
    else:
        root.setLevel(lvl)

    return logging.getLogger("astf")
