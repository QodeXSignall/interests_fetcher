"""Настройка логгера (единый файл на день)."""

from __future__ import annotations

from datetime import datetime
from logging import Formatter
from interests_fetcher.data import settings
import logging
import os


class DailyFileHandler(logging.FileHandler):
    """
    FileHandler, который пишет в journal_YYYY-MM-DD.log и в полночь
    автоматически переключается на файл нового дня.
    """

    def __init__(self, logs_dir: str, encoding: str = "utf-8"):
        self.logs_dir = logs_dir
        self._current_day = datetime.now().date()
        filename = self._build_filename(self._current_day)
        super().__init__(filename=filename, mode="a", encoding=encoding, delay=True)

    def _build_filename(self, day) -> str:
        os.makedirs(self.logs_dir, exist_ok=True)
        return os.path.join(self.logs_dir, f"journal_{day.isoformat()}.log")

    def emit(self, record):
        today = datetime.now().date()
        if today != self._current_day:
            self.acquire()
            try:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                self._current_day = today
                self.baseFilename = os.path.abspath(self._build_filename(today))
            finally:
                self.release()
        super().emit(record)


logging.getLogger("urllib3").setLevel(logging.INFO)

logger = logging.getLogger(__name__)
logger.propagate = False
if settings.config.getboolean("General", "debug"):
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = DailyFileHandler(logs_dir=settings.LOGS_DIR, encoding="utf-8")
    stream_handler = logging.StreamHandler()
    formatter = Formatter(fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
