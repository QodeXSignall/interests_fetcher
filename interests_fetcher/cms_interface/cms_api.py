from typing import Iterable, List, Dict, Tuple, Any
from interests_fetcher.cms_interface import functions
from interests_fetcher import functions as core_funcs
from interests_fetcher.cms_interface import cms_http
from interests_fetcher.cms_interface import limits
from interests_fetcher.logger import logger
from interests_fetcher.data import settings
from httpx import Response
import subprocess
import datetime
import asyncio
import shutil
import time
import cv2
import os

def _grab_frame_ffmpeg_to_bytes(input_path: str, mode: str) -> bytes | None:
    """
    Возвращает JPEG-байты кадра через image2pipe.
    mode: 'first' | 'last'
    """
    if mode == "first":
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", "0", "-i", input_path,
            "-frames:v", "1",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"
        ]
    elif mode == "last":
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-sseof", "-1", "-i", input_path,
            "-frames:v", "1",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"
        ]
    else:
        raise ValueError("mode must be 'first' or 'last'")

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode == 0 and res.stdout:
        return bytes(res.stdout)
    return None

def _extract_edge_frames_bytes_sync(video_path: str, channel_id: int, reg_id: str) -> tuple[tuple[str, bytes] | None, tuple[str, bytes] | None]:
    """
    Снимает кадры в память (JPEG bytes) без временных файлов.
    Возвращает кортежи вида: ('ch{ch}_first.jpg', bytes) и ('ch{ch}_last.jpg', bytes)
    или None, если кадр не получилось получить.
    """
    first_name = f"ch{channel_id}_first.jpg"
    last_name  = f"ch{channel_id}_last.jpg"

    # 1) Быстрый путь через ffmpeg → pipe
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
            return ((first_name, first_bytes) if first_bytes else None,
                    (last_name,  last_bytes) if last_bytes  else None)

    # 2) Fallback: OpenCV
    cap = None
    for attempt in range(3):
        cap = cv2.VideoCapture(video_path)
        if cap.isOpened():
            break
        logger.warning(f"{reg_id}. ch={channel_id} Попытка {attempt+1}: Не открыть видео {video_path}. Существует: {os.path.exists(video_path)}")
        time.sleep(0.2)

    if not cap or not cap.isOpened():
        logger.error(f"{reg_id}. ch={channel_id} Не удалось открыть видео: {video_path}")
        return None, None

    try:
        # первый кадр
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret_first, frame_first = cap.read()
        if ret_first and frame_first is not None:
            ok, buf = cv2.imencode(".jpg", frame_first)
            if ok:
                first_bytes = buf.tobytes()

        # последний кадр
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

        return ((first_name, first_bytes) if first_bytes else None,
                (last_name,  last_bytes)  if last_bytes  else None)
    finally:
        try:
            cap.release()
        except:
            pass

async def extract_edge_frames_bytes(video_path: str, channel_id: int, reg_id: str) -> tuple[tuple[str, bytes] | None, tuple[str, bytes] | None]:
    # ограничитель по кадрам оставляем
    async with limits.get_frame_sem():
        return await asyncio.to_thread(_extract_edge_frames_bytes_sync, video_path, channel_id, reg_id)


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

def _grab_frame_ffmpeg(input_path: str, output_path: str, mode: str) -> bool:
    """
    mode: 'first' | 'last'
    - 'first': первый кадр (быстро, без полного декодирования)
    - 'last' : последний кадр через -sseof -1
    """
    if mode == "first":
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "0", "-i", input_path,
            "-frames:v", "1",
            output_path
        ]
    elif mode == "last":
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-sseof", "-1", "-i", input_path,
            "-frames:v", "1",
            output_path
        ]
    else:
        raise ValueError("mode must be 'first' or 'last'")

    # Без shell=True — безопаснее и стабильнее на Windows/Linux
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0


class DeviceOfflineError(RuntimeError):
    """CMS: устройство офлайн — нужно отложить обработку интереса и попробовать позже."""
    pass


@functions.cms_data_get_decorator_async()
async def get_online_devices(jsession, device_id=None):
    url = f"{settings.cms_host}/StandardApiAction_getDeviceOlStatus.action?"
    params = {"jsession": jsession,
              "status": 1,
              "devIdno": device_id}
    async with limits.get_cms_global_sem():
        client = cms_http.get_cms_async_client()
        return await client.get(url, params=params)


@functions.cms_data_get_decorator_async()
async def get_offline_devices(jsession, device_id=None):
    url = f"{settings.cms_host}/StandardApiAction_getDeviceOlStatus.action?"
    params = {"jsession": jsession,
              "status": 0,
              "devIdno": device_id}
    async with limits.get_cms_global_sem():
        client = cms_http.get_cms_async_client()
        return await client.get(url, params=params)


@functions.cms_data_get_decorator_async()
async def login():
    url = f"{settings.cms_host}/StandardApiAction_login.action?"
    params = {"account": settings.cms_login,
                "password": settings.cms_password}
    async with limits.get_cms_global_sem():
        client = cms_http.get_cms_async_client()
        return await client.get(url, params=params)


@functions.cms_data_get_decorator_async()
async def get_video(jsession, device_id: str, start_time_seconds: int,
                    end_time_seconds: int, year: int, month: int, day: int,
                    channel_id: int = 0, fileattr: int = 2):
    params = {
        "DevIDNO": device_id, "LOC": 1, "CHN": channel_id,
        "YEAR": year, "MON": month, "DAY": day,
        "RECTYPE": -1, "FILEATTR": fileattr,
        "BEG": start_time_seconds, "END": end_time_seconds,
        "ARM1": 0, "ARM2": 0, "RES": 0, "STREAM": -1, "STORE": 0,
        "jsession": jsession, "DownType": 2
    }
    url = f"{settings.cms_host}/StandardApiAction_getVideoFileInfo.action"

    async with limits.get_cms_global_sem():
        async with limits.get_device_sem(device_id):
            client = cms_http.get_cms_async_client()
            return await client.get(url, params=params)


@functions.cms_data_get_decorator_async()
async def get_device_track_page_async(jsession: str, device_id: str,
                                      start_time: str, end_time: str,
                                      page: int | None = None) -> cms_http.httpx.Response:
    params = {
        "jsession": jsession,
        "devIdno": device_id,
        "begintime": start_time,
        "endtime": end_time,
    }
    if page is not None:
        params["currentPage"] = page
    url = f"{settings.cms_host}/StandardApiAction_queryTrackDetail.action"
    async with limits.get_cms_global_sem():
        async with limits.get_device_sem(device_id):
            client = cms_http.get_cms_async_client()
            resp =  await client.get(url, params=params)
            return resp


@functions.cms_data_get_decorator_async()
async def get_device_track(jsession: str, device_id: str, start_time: str,
                     stop_time: str, page: int | None = None):
    params = {
        "jsession": jsession,
        "devIdno": device_id,
        "begintime": start_time,
        "endtime": stop_time,
    }
    if page is not None:
        params["currentPage"] = page  # только если есть

    url = f"{settings.cms_host}/StandardApiAction_queryTrackDetail.action"
    async with limits.get_cms_global_sem():
        async with limits.get_device_sem(device_id):
            client = cms_http.get_cms_async_client()
            return await client.get(url, params=params)


async def get_device_track_all_pages_async(jsession: str, device_id: str, start_time: str, end_time: str) -> list[dict]:
    first = await get_device_track_page_async(jsession, device_id, start_time, end_time, page=None)
    first.raise_for_status()
    data = first.json()
    pages = int(data.get("pagination", {}).get("totalPages", 1)) or 1
    results = [data]
    if pages > 1:
        async def _fetch(p):
            async with limits.get_pages_sem():
                r = await get_device_track_page_async(jsession, device_id, start_time, end_time, page=p)
                r.raise_for_status()
                return r.json()

        tasks = [asyncio.create_task(_fetch(p)) for p in range(2, pages + 1)]
        results.extend(await asyncio.gather(*tasks))
    return results

@functions.cms_data_get_decorator_async()
async def get_device_status_async(jsession: str, device_id: str) -> Response:
    url = f"{settings.cms_host}/StandardApiAction_getDeviceStatus.action"
    params={"jsession": jsession, "devIdno": device_id}
    async with limits.get_cms_global_sem():
        client = cms_http.get_cms_async_client()
        return await client.get(url, params=params)

@functions.cms_data_get_decorator_async()
async def get_device_alarm_page_async(
    jsession: str,
    device_id: str,
    begin_time: str,
    end_time: str,
    arm_types: str = "19,20,69,70",
    page: int | None = None,
    page_records: int | None = None,
) -> cms_http.httpx.Response:
    """
    Запрос одной страницы алармов StandardApiAction_queryAlarmDetail.action

    Параметры пагинации:
      - currentPage: 1..N
      - pageRecords: кол-во записей на страницу (по умолчанию у CMS 10)
    """
    params: dict[str, Any] = {
        "jsession": jsession,
        "devIdno": device_id,
        "begintime": begin_time,
        "endtime": end_time,
        "armType": arm_types,
    }
    if page is not None:
        params["currentPage"] = page
    if page_records is not None:
        params["pageRecords"] = page_records

    url = f"{settings.cms_host}/StandardApiAction_queryAlarmDetail.action"

    async with limits.get_cms_global_sem():
        async with limits.get_device_sem(device_id):
            client = cms_http.get_cms_async_client()
            return await client.get(url, params=params)


async def get_device_alarm_all_pages_async(
    jsession: str,
    device_id: str,
    begin_time: str,
    end_time: str,
    arm_types: str = "19,20,69,70",
    page_records: int = 200,
) -> list[dict]:
    """
    Возвращает список JSON-страниц (как у get_device_track_all_pages_async).
    Страница = dict с ключами "alarms", "pagination", "result", ...

    Использует параллельную догрузку остальных страниц через limits.get_pages_sem().
    """
    # 1) Первая страница — чтобы узнать totalPages
    first = await get_device_alarm_page_async(
        jsession, device_id, begin_time, end_time, arm_types,
        page=None,  # пусть сервер подставит 1 по умолчанию
        page_records=page_records
    )
    first.raise_for_status()
    data = first.json()
    pages = int((data.get("pagination") or {}).get("totalPages", 1)) or 1

    results: list[dict] = [data]
    if pages > 1:
        async def _fetch(p: int) -> dict:
            async with limits.get_pages_sem():
                r = await get_device_alarm_page_async(
                    jsession, device_id, begin_time, end_time, arm_types,
                    page=p, page_records=page_records
                )
                r.raise_for_status()
                return r.json()

        tasks = [asyncio.create_task(_fetch(p)) for p in range(2, pages + 1)]
        results.extend(await asyncio.gather(*tasks))

    return results


def flatten_alarms_pages(pages: list[dict]) -> list[dict]:
    """
    Удобный хелпер: склеить все "alarms" из списка страниц,
    с лёгкой дедупликацией по (guid or (atp, stm, etm, chn)).
    """
    merged: list[dict] = []
    seen: set[tuple] = set()

    for page in pages:
        for a in (page.get("alarms") or []):
            key = (
                a.get("guid")
                or (
                    a.get("atp"),
                    a.get("stm"),
                    a.get("etm"),
                    a.get("chn") or a.get("channel") or 0,
                )
            )
            if key not in seen:
                seen.add(key)
                merged.append(a)

    # Отсортируем по началу/концу (UTC сек)
    merged.sort(key=lambda x: (x.get("stm", 0), x.get("etm", 0)))
    return merged

@functions.cms_data_get_decorator_async()
async def execute_download_task(jsession, download_task_url: str, reg_id):
    #params={"jsession": jsession}
    #logger.debug("execute_download_task", jsession, params, download_task_url)
    async with limits.get_cms_global_sem():
        async with limits.get_device_sem(reg_id):
            client = cms_http.get_cms_async_client()
            return await client.get(download_task_url)


async def wait_and_get_dwn_url(jsession, download_task_url, reg_id, poll_interval=1.0, timeout=1800.0,
                               interest_name:str = "ND", channel_id:int = 0):
    logger.info(f"{reg_id}:{interest_name} ch{channel_id}  Загрузка видео...")
    started = time.monotonic()
    count = 0
    while True:
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"{reg_id}:{interest_name} ch{channel_id}  download task timed out after {timeout}s")

        response = await execute_download_task(jsession=jsession, download_task_url=download_task_url, reg_id=reg_id)
        response_json = response.json()
        if not response_json:
            await asyncio.sleep(poll_interval); continue

        result = response_json.get("result")
        old = (response_json.get("oldTaskAll") or {})
        dph = old.get("dph")

        if result == 11 and dph:
            logger.info(f"{reg_id}:{interest_name} ch{channel_id}. Загрузка видео завершена!")
            logger.debug(f"Get path: {dph}")
            if not os.path.exists(dph):
                logger.error(f"{reg_id}:{interest_name} ch{channel_id}. При этом фактически файла нет на диске! ({response_json})")
            return dph
        if result == 32:
            logger.warning(f"{reg_id}:{interest_name} ch{channel_id}. Устройство отключено! 32")
            raise DeviceOfflineError

        count += 1
        if count % 60 == 0:
            logger.info(f"{reg_id}:{interest_name} ch{channel_id} . Все еще грузится: {response_json}. Уже {count} сек.")
        await asyncio.sleep(poll_interval)


async def download_video(
    jsession,
    reg_id: str,
    channel_id: int,
    year: int,
    month: int,
    day: int,
    start_sec: int,
    end_sec: int,
    adjustment_sequence: Iterable[int] = (0, 30, 60, 90),
    interest_name: str = "ND",
):
    """
    Правка: при result=22 ('device no response') НЕ двигаем окно.
    Повторяем попытки на том же интервале с экспоненциальным backoff,
    и только после исчерпания лимита переходим к следующему delta.
    """
    start_limit, end_limit = 0, 24 * 60 * 60 - 1
    base_start, base_end = int(start_sec), int(end_sec)
    file_paths: List[str] = []

    # --- настройки повтора именно для result=22 ---
    MAX_NO_RESPONSE_RETRIES = 5        # сколько раз пробуем то же окно
    BACKOFF_START = 2.0                # секунд
    BACKOFF_MULT = 1.5
    BACKOFF_MAX = 15.0

    # превратим в список, чтобы можно было логировать длину
    adj = list(adjustment_sequence)
    if not adj or adj[0] != 0:
        adj = [0] + adj  # гарантируем первую попытку без расширения

    for i, delta in enumerate(adj, start=1):
        # вычисляем текущее окно; НЕ меняем base_* — только расширения по delta
        cur_start = max(start_limit, base_start - delta)
        cur_end   = min(end_limit,   base_end + delta)

        logger.debug(
            f"{reg_id}:{interest_name} ch{channel_id} попытка {i}/{len(adj)} — window=[{cur_start}..{cur_end}] "
            f"(base=[{base_start}..{base_end}], Δ={delta})"
        )

        # Повторы НА ТОМ ЖЕ Δ при проблемах с ответом устройства
        no_resp_attempt = 0
        backoff = BACKOFF_START

        while True:
            # --- две попытки на том же delta просто на случай кривого JSON ---
            for _ in range(2):
                response = await get_video(
                    jsession, reg_id, cur_start, cur_end,
                    year, month, day, channel_id
                )
                try:
                    response_json = response.json()
                    break
                except Exception as e:
                    logger.warning(
                        f"{reg_id}:{interest_name} ch{channel_id} JSON parse failed on window [{cur_start}..{cur_end}]: {e}; "
                        f"retry same delta"
                    )
                    await asyncio.sleep(2)
            else:
                # обе попытки распарсить JSON не удались — это не 'device no response',
                # двигаемся к следующему delta
                break

            result = response_json.get("result")
            message = response_json.get("message", "") or ""
            files = response_json.get("files") or []

            logger.debug(
                f"{reg_id}:{interest_name} ch{channel_id} get_video result={result}, msg={message!r}, files={len(files)}"
            )

            # устройство реально офлайн — выходим вверх по стеку
            if result == 32 and "Device is not online" in message:
                logger.warning(f"{reg_id}:{interest_name} ch{channel_id}  устройство офлайн")
                raise DeviceOfflineError(message or "Device is not online!")

            if result == 23 and "device offline" in message:
                logger.warning(f"{reg_id}:{interest_name} ch{channel_id}  устройство офлайн")
                raise DeviceOfflineError(message or "Device is not online!")

            # наш кейс: устройство «не ответило» — НЕ двигаем окно, повторяем то же
            if result == 22 and "device no response" in message.lower():
                if no_resp_attempt < MAX_NO_RESPONSE_RETRIES:
                    logger.debug(
                        f"{reg_id}:{interest_name} ch{channel_id} device no response — retry same window "
                        f"[{cur_start}..{cur_end}] in {backoff:.1f}s "
                        f"({no_resp_attempt+1}/{MAX_NO_RESPONSE_RETRIES})"
                    )
                    await asyncio.sleep(backoff)
                    no_resp_attempt += 1
                    backoff = min(BACKOFF_MAX, backoff * BACKOFF_MULT)
                    continue  # ВАЖНО: остаёмся на том же delta, не расширяем окно
                else:
                    logger.warning(
                        f"{reg_id}:{interest_name} ch{channel_id} device no response — exhausted retries on the SAME window "
                        f"[{cur_start}..{cur_end}]; move to next delta"
                    )
                    # выходим из while -> перейдём к следующему delta
                    break

            # если пришли файлы — забираем и выходим
            if files:
                for f in files:
                    url = f.get("DownTaskUrl")
                    if not url:
                        logger.warning(f"{reg_id}: у файла нет DownTaskUrl: {f}")
                        continue
                    try:
                        file_path = await wait_and_get_dwn_url(
                            jsession=jsession, download_task_url=url, reg_id=reg_id, channel_id=channel_id,
                            interest_name=interest_name)
                    except TimeoutError:
                        logger.error("Timeout error!")
                        raise DeviceOfflineError("Timeout error")
                    if file_path:
                        file_paths.append(file_path)
                return file_paths or None

            # Иные случаи: нет файлов, другие коды и т.д. — не зацикливаемся на этом delta,
            # выходим к следующему delta (расширяем окно по старой логике)
            await asyncio.sleep(2)
            break

    logger.warning(
        f"{reg_id}: файлы не найдены после {len(adj)} попыток. "
        f"Последнее окно было [{cur_start}..{cur_end}]"
    )
    return None


async def download_single_clip_per_channel(
    jsession: str,
    reg_id: str,
    interest: dict,
    channels: list[int] = (0, 1, 2, 3)):
    """
    Скачивает РОВНО ОДИН финальный видеоклип на каждый канал так,
    чтобы в нём попадали и начало, и конец интереса.
    Если CMS отдаёт несколько отрезков — конкатенируем в один файл.
    Возвращает: {ch: {"path": str|None, "concat_sources": list[str]|None}}
    """
    TIME_FMT = "%Y-%m-%d %H:%M:%S"
    dt_start = datetime.datetime.strptime(interest["start_time"], TIME_FMT)
    dt_end   = datetime.datetime.strptime(interest["end_time"],   TIME_FMT)
    #raw_start = interest["start_time"]
    #if isinstance(raw_start, datetime.datetime):
    #    dt_start = raw_start
    #else:
    #    dt_start = datetime.datetime.strptime(raw_start, TIME_FMT)
    #
    #raw_end = interest["end_time"]
    #if isinstance(raw_end, datetime.datetime):
    #    dt_end = raw_end
    #else:
    #    dt_end = datetime.datetime.strptime(raw_end, TIME_FMT)
    interest_name = interest["name"]

    start_sec = dt_start.hour * 3600 + dt_start.minute * 60 + dt_start.second
    end_sec   = dt_end.hour   * 3600 + dt_end.minute   * 60 + dt_end.second

    out: Dict[int, Dict[str, Any]] = {}
    interest_tmp_dir = os.path.join(settings.TEMP_FOLDER, interest["name"])
    os.makedirs(interest_tmp_dir, exist_ok=True)

    async def _one_channel(ch: int, interest_name: str) -> Tuple[int, str | None, list[str] | None]:
        # Скачиваем все куски на интервале, дальше сведём в один файл
        async with limits._get_video_sem_for(reg_id):
            videos_paths = await download_video(
                jsession=jsession,
                reg_id=reg_id,
                channel_id=ch,
                year=dt_start.year, month=dt_start.month, day=dt_start.day,
                start_sec=start_sec,
                end_sec=end_sec,
                adjustment_sequence=(0, 5, 10, 15, 30),
                interest_name=interest_name,
            )  # уже есть в проекте  :contentReference[oaicite:1]{index=1}

        if not videos_paths:
            logger.warning(f"{reg_id}: ch={ch} клипы не получены.")
            return ch, None, videos_paths

        if len(videos_paths) == 1:
            return ch, videos_paths[0], videos_paths

        # конкат в один файл (mp4) тем же методом, что используешь для интересов
        merged_path = os.path.join(interest_tmp_dir, f"ch{ch}_merged.mp4")
        try:
            await asyncio.to_thread(core_funcs.concatenate_videos, videos_paths, merged_path, reg_id, interest_name)  # :contentReference[oaicite:2]{index=2}
            return ch, merged_path, videos_paths
        except Exception as e:
            logger.error(f"{reg_id}: {interest_name} ch={ch} concat failed: {e}")
            return ch, None, videos_paths

    tasks = [asyncio.create_task(_one_channel(ch,  interest_name)) for ch in channels]
    for t in asyncio.as_completed(tasks):
        ch, path, videos_paths = await t
        out[ch] = {"path": path,
                   "concat_sources": videos_paths}
    return out

def delete_videos_except(
    videos_by_channel: Dict[int, str | None],
    keep_channel_id: int | None
) -> int:
    """
    Удаляет все локальные видео, кроме выбранного канала (если указан).
    Возвращает кол-во удалённых файлов.
    """
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

