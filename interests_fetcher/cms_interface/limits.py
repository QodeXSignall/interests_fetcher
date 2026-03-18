# limits.py
from interests_fetcher.data.settings import config
from interests_fetcher.logger import logger
from typing import Dict
import asyncio

def _safe_int(section: str, key: str, default: int) -> int:
    try:
        v = int(config.get(section, key, fallback=str(default)))
        if v < 1:
            logger.warning(f"[{section}] {key}={v} < 1, принудительно -> 1")
            return 1
        return v
    except Exception as e:
        logger.warning(f"[{section}] {key}: ошибка чтения ({e}), fallback -> {default}")
        return default

# ленивые синглтоны
_global_cms_sem: asyncio.Semaphore | None = None
_frame_sem: asyncio.Semaphore | None = None
_pages_sem: asyncio.Semaphore | None = None

# per-device
_device_sems: Dict[str, asyncio.Semaphore] = {}
_get_video_locks: Dict[str, asyncio.Semaphore] = {}

def get_cms_global_sem() -> asyncio.Semaphore:
    global _global_cms_sem
    if _global_cms_sem is None:
        max_conc = _safe_int("Process", "MAX_CMS_CONCURRENT", 8)
        _global_cms_sem = asyncio.BoundedSemaphore(max_conc)
    return _global_cms_sem

def get_device_sem(device_id: str) -> asyncio.Semaphore:
    sem = _device_sems.get(device_id)
    if sem is None:
        per_dev = _safe_int("Process", "MAX_CMS_PER_DEVICE", 2)
        sem = asyncio.BoundedSemaphore(per_dev)
        _device_sems[device_id] = sem
    return sem

def get_frame_sem() -> asyncio.Semaphore:
    global _frame_sem
    if _frame_sem is None:
        max_frames = _safe_int("Process", "MAX_FRAME_EXTRACT", 4)
        _frame_sem = asyncio.BoundedSemaphore(max_frames)
    return _frame_sem

def _get_video_sem_for(dev_id: str) -> asyncio.Semaphore:
    """
    Ограничивает число параллельных getVideoFileInfo/скачиваний для одного устройства.
    """
    sem = _get_video_locks.get(dev_id)
    if sem is None:
        per_dev = _safe_int("Process", "MAX_DOWNLOADS_PER_DEVICE", 1)
        sem = asyncio.BoundedSemaphore(per_dev)
        _get_video_locks[dev_id] = sem
    return sem

def get_pages_sem() -> asyncio.Semaphore:
    global _pages_sem
    if _pages_sem is None:
        _pages_sem = asyncio.BoundedSemaphore(_safe_int("Semafor", "tracks_page_request_max", 4))
    return _pages_sem
