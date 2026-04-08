import os
import json
import tempfile
import logging

logger = logging.getLogger(__name__)

# --- Resolve settings.states path -------------------------------------------------
# Пытаемся взять путь к states из твоего settings. Если не выйдет — используем env/дефолт.

from interests_fetcher.data import settings as _settings

if _settings and getattr(_settings, "states", None):
    STATES_PATH = _settings.states
else:
    STATES_PATH = os.environ.get("QT_PVP_STATES_PATH", os.path.abspath("data/states.json"))

LOCK_PATH = STATES_PATH + ".lock"

# --- Cross-platform file lock -----------------------------------------------------
# Пытаемся использовать portalocker. Если его нет — fallback на msvcrt/fcntl.
_HAVE_PORTALOCKER = False
try:
    import portalocker  # type: ignore
    _HAVE_PORTALOCKER = True
except Exception:
    portalocker = None  # type: ignore

if os.name == "nt":
    # Windows fallback
    try:
        import msvcrt  # type: ignore
    except Exception as e:
        raise RuntimeError("On Windows you need either portalocker or msvcrt") from e
else:
    # POSIX fallback
    try:
        import fcntl  # type: ignore
    except Exception as e:
        raise RuntimeError("On POSIX you need either portalocker or fcntl") from e


class FileLock:
    """
    Межпроцессный эксклюзивный лок на отдельный lock-файл.
    Использует:
      - portalocker (если установлен), иначе
      - msvcrt.locking на Windows, иначе
      - fcntl.flock на POSIX.
    """
    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def __enter__(self):
        # бинарный режим, чтобы одинаково работать с msvcrt/portalocker
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._fh = open(self.path, "a+b")
        if _HAVE_PORTALOCKER:
            portalocker.lock(self._fh, portalocker.LOCK_EX)
        else:
            if os.name == "nt":
                # Лочим 1 байт; для lock-файла этого достаточно
                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
                except OSError:
                    # создаём файл, если пустой
                    self._fh.write(b"\0")
                    self._fh.flush()
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if _HAVE_PORTALOCKER:
                portalocker.unlock(self._fh)
            else:
                if os.name == "nt":
                    self._fh.seek(0)
                    try:
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self._fh.close()
            except Exception:
                pass


# --- Safe load/save of states -----------------------------------------------------

def _load_states() -> dict:
    """
    Безопасная загрузка JSON-состояний.
    Если файл отсутствует — вернём минимальную структуру.
    """
    try:
        with open(STATES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"trucks": {}}
    except json.JSONDecodeError as e:
        # Коррупт. Логируем и пробуем не дать упасть — отдаём пустую структуру.
        logger.error("states.json is corrupted: %s", e)
        return {"trucks": {}}

    if not isinstance(data, dict):
        return {"trucks": {}}
    data.setdefault("trucks", {})
    # Legacy regs → trucks (однократно; сохраняем сразу после миграции)
    try:
        from interests_fetcher.truck_state import consolidate_trucks_by_mdvr, migrate_regs_to_trucks_if_needed

        migrated = migrate_regs_to_trucks_if_needed(data)
        consolidate_trucks_by_mdvr(data)
        if migrated:
            _atomic_save_states(data)
    except Exception as e:
        logger.error("states migration failed: %s", e)
    return data


def _sanitize_for_json(obj):
    """
    Рекурсивно проходит по структуре и превращает всё несерилизуемое в сериализуемое.
    datetime -> "%Y-%m-%d %H:%M:%S"
    set -> list
    остальное оставляем как есть.
    """
    import datetime as _dt

    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]

    if isinstance(obj, tuple):
        return tuple(_sanitize_for_json(v) for v in obj)

    if isinstance(obj, set):
        # множества у нас бывают (io_indices_set в алармах), JSON их не любит
        return [_sanitize_for_json(v) for v in obj]

    if isinstance(obj, _dt.datetime):
        # без tzinfo, нам и не надо её тут
        return obj.strftime("%Y-%m-%d %H:%M:%S")

    # числа, строки, bool, None проходят как есть
    return obj


def _atomic_save_states(states: dict) -> None:
    """
    Атомарная запись JSON:
      1) приводим к сериализуемому виду (без datetime)
      2) пишем во временный файл в той же директории
      3) fsync
      4) os.replace -> атомарная подмена целевого файла
    """
    dir_ = os.path.dirname(STATES_PATH) or "."
    os.makedirs(dir_, exist_ok=True)

    safe_states = _sanitize_for_json(states)

    fd, tmp_path = tempfile.mkstemp(prefix=".states.", suffix=".tmp", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(safe_states, tmp, indent=4, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, STATES_PATH)
    finally:
        # если replace не сработал — подчистим tmp
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


__all__ = ["FileLock", "_load_states", "_atomic_save_states", "LOCK_PATH", "STATES_PATH"]
