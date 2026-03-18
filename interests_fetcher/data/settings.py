import importlib.util, os
import configparser
import posixpath
import re

_top_pkg = (__package__ or "interests_fetcher").split(".", 1)[0]
spec = importlib.util.find_spec(_top_pkg)
if not spec or not spec.origin:
    raise RuntimeError(f"Не найден пакет {_top_pkg}")
CUR_DIR = os.path.dirname(spec.origin)  # путь к .../interests_fetcher

OUTPUT_FOLDER = os.path.join(CUR_DIR, "output")
INPUT_FOLDER = os.path.join(CUR_DIR, "input")
TESTS_FOLDER = os.path.join(CUR_DIR, "tests")
TEMP_FOLDER = os.path.join(CUR_DIR, "temp")
DATA_FOLDER = os.path.join(CUR_DIR, "data")
FRAMES_TEMP_FOLDER = os.path.join(TEMP_FOLDER, "frames")
REPORTS_TEMP_FOLDER = os.path.join(TEMP_FOLDER, "reports")
INTERESTING_VIDEOS_FOLDER = os.path.join(CUR_DIR, "interesting_videos")
TESTS_MISC_FOLDER = os.path.join(TESTS_FOLDER, "misc")
IGNORE_POINTS_JSON = os.path.join(DATA_FOLDER, "ignore_points.json")
LOGS_DIR = os.path.join(CUR_DIR, "logs")
CONFIG_PATH = os.path.join(DATA_FOLDER, "config.cfg")
CLOUD_PATH = posixpath.join("/Tracker", "Видео выгрузок")
states = os.sep.join((DATA_FOLDER, "states.json"))

config = configparser.ConfigParser(
    inline_comment_prefixes='#',
    allow_no_value=True)
config.read(CONFIG_PATH, encoding="utf-8")


def _cfg_get(section: str, key: str, fallback: str = "") -> str:
    if not config.has_section(section):
        return fallback
    return config.get(section, key, fallback=fallback)


def _cfg_getint(section: str, key: str, fallback: int) -> int:
    if not config.has_section(section):
        return fallback
    return config.getint(section, key, fallback=fallback)


# Legacy CMS vars are kept for backward compatibility only.
# The primary integration path is cms_gate via CMS_GATE_BASE_URL.
cms_host = os.environ.get("CMS_HOST", "").rstrip("/")
if not cms_host:
    schema = _cfg_get("CMS", "schema", "")
    ip = _cfg_get("CMS", "ip", "")
    port = _cfg_getint("CMS", "port", 0)
    if schema and ip and port:
        cms_host = f"{schema}{ip}:{port}"
cms_login = os.environ.get("cms_login")
cms_password = os.environ.get("cms_password")

qt_rm_url = (f"{_cfg_get('QT_RM', 'schema')}{_cfg_get('QT_RM', 'host')}:"
             f"{_cfg_get('QT_RM', 'port')}")
qt_rm_login = os.environ.get("qt_rm_login")
qt_rm_password = os.environ.get("qt_rm_password")

TIME_FMT = "%Y-%m-%d %H:%M:%S"

# Фича-флаг: использовать ли внешний сервис cms_gate вместо прямых вызовов cms_api.
USE_CMS_GATE = os.environ.get("USE_CMS_GATE", "").lower() in ("1", "true", "yes")


_INTEREST_RE = re.compile(
    r"""
    ^
    (?P<plate>.+?)          # всё до подчёркивания — номер/идентификатор, допускаем пробелы
    _
    (?P<date>\d{4}\.\d{2}\.\d{2})   # YYYY.MM.DD
    \s
    (?P<start>\d{2}\.\d{2}\.\d{2})  # HH.MM.SS
    -
    (?P<end>\d{2}\.\d{2}\.\d{2})    # HH.MM.SS
    (?:\.[A-Za-z0-9]{1,4})?         # опц. расширение (на всякий)
    $
    """,
    re.VERBOSE
)
