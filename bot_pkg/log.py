import logging
from logging.handlers import RotatingFileHandler

from . import config

_configured = False

def setup():
    """Configures the root logger once. Console output at LOG_LEVEL (default
    INFO), plus a rotating file at data/logs/bot.log so you can tail it or
    pull it off a headless host after the fact."""
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)-22s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        fileh = RotatingFileHandler(config.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError as e:
        logging.getLogger(__name__).warning(f"Couldn't open log file {config.LOG_FILE}: {e}")

    # Pyrogram is chatty at INFO (session/connection housekeeping); keep it
    # at WARNING unless you're actually debugging Pyrogram itself.
    logging.getLogger("pyrogram").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(f"Logging ready — level={config.LOG_LEVEL}, file={config.LOG_FILE}")

def get(name):
    return logging.getLogger(name)
