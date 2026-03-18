""" Настройка логгера """

from logging.handlers import TimedRotatingFileHandler
from logging import Formatter
from interests_fetcher.data import settings
import logging
import time
import os


class CustomTimedRotatingFileHandler(TimedRotatingFileHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.suffix = "%Y-%m-%d"

    def namer(self, default_name):
        dir_name = os.path.dirname(default_name)
        base, ext = os.path.splitext(default_name)
        rollover_time = time.strftime(self.suffix,
                                      time.localtime(self.rolloverAt))
        return os.path.join(dir_name, f"journal_{rollover_time}.log")


logging.getLogger("urllib3").setLevel(logging.INFO)

logger = logging.getLogger(__name__)
if settings.config.getboolean("General", "debug"):
    logger.setLevel(logging.DEBUG)

log_filename = os.path.join(settings.LOGS_DIR, f'journal_{os.getpid()}.log')
handler = CustomTimedRotatingFileHandler(
    filename=log_filename,
    when='midnight',
    backupCount=60,
    encoding='utf-8',
    delay=True)

stream_handler = logging.StreamHandler()
formatter = Formatter(
    fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

logger.addHandler(handler)
logger.addHandler(stream_handler)
