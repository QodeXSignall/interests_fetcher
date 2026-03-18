import os
import shutil
import subprocess
import time
from typing import Tuple

import cv2

from interests_fetcher.logger import logger
from interests_fetcher.cms_interface import limits


def _grab_frame_ffmpeg_to_bytes(input_path: str, mode: str) -> bytes | None:
    if mode == "first":
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-i",
            input_path,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
    elif mode == "last":
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-sseof",
            "-1",
            "-i",
            input_path,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]
    else:
        raise ValueError("mode must be 'first' or 'last'")

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode == 0 and res.stdout:
        return bytes(res.stdout)
    return None


def _extract_edge_frames_bytes_sync(
    video_path: str, channel_id: int, reg_id: str
) -> Tuple[Tuple[str, bytes] | None, Tuple[str, bytes] | None]:
    first_name = f"ch{channel_id}_first.jpg"
    last_name = f"ch{channel_id}_last.jpg"

    first_bytes = last_bytes = None
    if _ffmpeg_available():
        try:
            first_bytes = _grab_frame_ffmpeg_to_bytes(video_path, "first")
        except Exception:
            first_bytes = None
        try:
            last_bytes = _grab_frame_ffmpeg_to_bytes(video_path, "last")
        except Exception:
            last_bytes = None
        if first_bytes or last_bytes:
            return (
                (first_name, first_bytes) if first_bytes else None,
                (last_name, last_bytes) if last_bytes else None,
            )

    cap = None
    for attempt in range(3):
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            break
        logger.warning(
            f"{reg_id}. ch={channel_id} Попытка {attempt+1}: Не открыть видео {video_path}. "
            f"Существует: {os.path.exists(video_path)}"
        )
        time.sleep(0.2)

    if not cap or not cap.isOpened():
        logger.error(f"{reg_id}. ch={channel_id} Не удалось открыть видео: {video_path}")
        return None, None

    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret_first, frame_first = cap.read()
        if ret_first and frame_first is not None:
            ok, buf = cv2.imencode(".jpg", frame_first)
            if ok:
                first_bytes = buf.tobytes()

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(total - 1, 0))
        ret_last, frame_last = cap.read()
        if (not ret_last or frame_last is None) and total > 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(total - 2, 0))
            ret_last, frame_last = cap.read()
        if ret_last and frame_last is not None:
            ok, buf = cv2.imencode(".jpg", frame_last)
            if ok:
                last_bytes = buf.tobytes()

        return (
            (first_name, first_bytes) if first_bytes else None,
            (last_name, last_bytes) if last_bytes else None,
        )
    finally:
        try:
            cap.release()
        except Exception:
            pass


async def extract_edge_frames_bytes(
    video_path: str, channel_id: int, reg_id: str
) -> Tuple[Tuple[str, bytes] | None, Tuple[str, bytes] | None]:
    async with limits.get_frame_sem():
        from asyncio import to_thread

        return await to_thread(_extract_edge_frames_bytes_sync, video_path, channel_id, reg_id)


def delete_videos_except(videos_by_channel: dict[int, str | None], keep_channel_id: int | None) -> int:
    removed = 0
    for ch, p in (videos_by_channel or {}).items():
        if not p:
            continue
        if keep_channel_id is not None and ch == keep_channel_id:
            continue
        try:
            if os.path.exists(p):
                os.remove(p)
                removed += 1
        except Exception as e:
            logger.warning(f"Не удалось удалить {p}: {e}")
    return removed


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

