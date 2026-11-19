"""Robust plain-text logging for long-running container workers."""
import logging
import sys


def setup_logging(level: str = "INFO"):
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        root.addHandler(handler)
    root.setLevel(lvl)
    return logging.getLogger("astf")
