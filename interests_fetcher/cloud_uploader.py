from webdav3.exceptions import RemoteResourceNotFound
from interests_fetcher.meta_cache import meta_cache
from webdav3.client import Client
from interests_fetcher.logger import logger
from urllib.parse import quote
from interests_fetcher.data import settings
from interests_fetcher import functions
import traceback
import posixpath
import requests
import asyncio
import random
import json
import time
import uuid
import os


LIST_TTL  = getattr(settings, "WEBDAV_LIST_TTL", 20)   # сек
CHECK_TTL = getattr(settings, "WEBDAV_CHECK_TTL", 60)  # сек
WEBDAV_META_RETRIES = int(getattr(settings, "WEBDAV_META_RETRIES", 4))
WEBDAV_META_BASE_DELAY_SEC = float(getattr(settings, "WEBDAV_META_BASE_DELAY_SEC", 0.6))


async def _webdav_call_with_retries(func, op_name: str, target: str):
    """
    Унифицированный retry для коротких мета-операций WebDAV (list/check).
    Держим его в async-слое, а блокирующий webdav3-вызов уводим в thread.
    """
    last_exc = None
    for attempt in range(1, WEBDAV_META_RETRIES + 1):
        try:
            return await asyncio.to_thread(func, target)
        except Exception as e:
            last_exc = e
            if attempt >= WEBDAV_META_RETRIES:
                break
            sleep_for = WEBDAV_META_BASE_DELAY_SEC * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            logger.warning(
                f"[WEBDAV][{op_name}] fail {attempt}/{WEBDAV_META_RETRIES} for '{target}': {e}. "
                f"retry in {sleep_for:.2f}s"
            )
            await asyncio.sleep(sleep_for)
    raise last_exc if last_exc else RuntimeError(f"[WEBDAV][{op_name}] unknown error for '{target}'")

async def aupload_dict_as_json_to_cloud(data: dict,
                                        remote_folder_path: str,
                                        filename: str = "report.json") -> bool:
    """
    Полностью async: без временных файлов, с корректной инвалидацией кэша.
    """
    try:
        # гарантируем папку (async-вариант!)
        ok = await acreate_folder_if_not_exists(client, remote_folder_path)
        if not ok:
            return False

        # собираем JSON в память
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        remote_file_path = posixpath.join(remote_folder_path, filename)

        # грузим байты через PUT (в thread, чтобы не блокировать loop)
        ok = await asyncio.to_thread(
            upload_bytes_to_cloud,
            client,
            payload,
            remote_file_path,
            "application/json; charset=utf-8",
        )
        if ok:
            # корректная async-инвалидация
            await ainvalidate_folder(remote_folder_path, meta_cache)
            await ainvalidate_path(remote_file_path, meta_cache)
        return ok

    except Exception as e:
        logger.error(f"[JSON-UPLOAD] Ошибка выгрузки {filename} в {remote_folder_path}: {e}")
        return False

def _list_cloud_interest_folders_for_day(client, plate: str, day_str: str) -> list[str]:
    """
    Возвращает список имён интересов (папок) на WebDAV для заданного госномера и дня.
    Путь: {CLOUD_BASE_PATH}/{plate}/{day_str}/<interest_name>/
    """
    day_path = f"{settings.CLOUD_PATH}/{plate}/{day_str}"
    try:
        items = client.list(day_path)
    except RemoteResourceNotFound:
        return []
    except Exception as e:
        logger.warning(f"[WEBDAV] Ошибка при list('{day_path}'): {e}")
        return []

    names: list[str] = []
    for item in items:
        name = item.rstrip("/").split("/")[-1]
        if "_" in name and "-" in name and "." in name:
            names.append(name)
    return sorted(names)


def upload_bytes_to_cloud(client, data: bytes, remote_path: str, content_type: str = "application/octet-stream",
                          retries: int = 5, base_delay: float = 0.8) -> bool:
    """
    Сырая загрузка байт через HTTP PUT (минуя локальные файлы).
    Использует настройки и сессию webdav3-клиента.
    """
    full_url = _build_full_url(client, remote_path)
    auth = _resolve_auth(client)
    headers = {"Content-Type": content_type}

    # Гарантируем, что родительская папка существует
    parent = posixpath.dirname(remote_path)
    create_folder_if_not_exists(client, parent)

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            sess = getattr(client, "session", None) or requests.Session()
            resp = sess.put(full_url, data=data, headers=headers, auth=auth)
            if 200 <= resp.status_code < 300:
                logger.info(f"[PUT BYTES] {remote_path}: OK ({len(data)} bytes)")
                try:
                    invalidate_folder_now(parent, meta_cache)
                    invalidate_path_now(remote_path, meta_cache)
                except Exception:
                    pass
                return True
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            last_exc = e
            logger.warning(f"[PUT BYTES] fail {attempt}/{retries} → {remote_path}: {e}")
            if attempt >= retries:
                break
            time.sleep(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.3))
    if last_exc:
        logger.error(f"[PUT BYTES] give up: {remote_path}: {last_exc}")
    return False

async def upload_many_bytes_async(items: list[tuple[str, bytes]], destination_folder: str,
                                  content_type: str = "image/jpeg", concurrency: int = 6) -> bool:
    """
    items: список кортежей (filename, bytes). Грузит параллельно, без временных файлов.
    """
    if not items:
        await ainvalidate_folder(destination_folder, meta_cache)
        return True

    sem = asyncio.Semaphore(concurrency)

    async def one(name: str, data: bytes) -> bool:
        if not data:
            return True
        remote_path = posixpath.join(destination_folder, name)
        async with sem:
            return await asyncio.to_thread(upload_bytes_to_cloud, client, data, remote_path, content_type)

    ok_list = await asyncio.gather(*(one(n, d) for (n, d) in items), return_exceptions=False)
    await ainvalidate_folder(destination_folder, meta_cache)

    return all(ok_list)


def _cache_key_list(folder: str) -> str:
    return f"dav:list:{folder.rstrip('/')}/"

def _cache_key_check(path: str) -> str:
    return f"dav:check:{path}"

async def cached_list(client, folder: str):
    key = _cache_key_list(folder)
    cached = await meta_cache.get(key)
    if cached is not None:
        return cached
    # webdav3: client.list может кидать таймауты/сетевые ошибки, делаем retry.
    items = (await _webdav_call_with_retries(client.list, "list", folder)) or []
    await meta_cache.set(key, items, LIST_TTL)
    return items

async def cached_check(client, path: str) -> bool:
    key = _cache_key_check(path)
    cached = await meta_cache.get(key)
    if cached is not None:
        return cached
    ok = bool(await _webdav_call_with_retries(client.check, "check", path))
    await meta_cache.set(key, ok, CHECK_TTL)
    return ok



async def create_interest_folder_path_async(name, dest):
    return await asyncio.to_thread(create_interest_folder_path, name, dest)

async def interest_video_exists_async(name):
    return await check_if_interest_video_exists(name)

async def upload_dict_as_json_to_cloud_async(data, remote_folder_path):
    return await asyncio.to_thread(upload_dict_as_json_to_cloud, data, remote_folder_path)

async def append_report_line_to_cloud_async(remote_folder_path, created_start_time, created_end_time, file_name):
    return await asyncio.to_thread(append_report_line_to_cloud, remote_folder_path, created_start_time, created_end_time, file_name)


class CloudOffline(RuntimeError):
    """CMS: устройство офлайн — нужно отложить обработку интереса и попробовать позже."""
    pass

# Настройки подключения к WebDAV серверу
options = {
    'webdav_hostname': os.environ.get("webdav_hostname"),
    'webdav_login': os.environ.get("webdav_login"),
    'webdav_password': os.environ.get("webdav_password")
}

client = Client(options)

def _resolve_webdav_base_and_root(client):
    base = ''
    root = ''

    if hasattr(client, "options"):
        try:
            base = (client.options.get("webdav_hostname") or "").rstrip("/")
            root = (client.options.get("webdav_root") or "").strip("/")
        except Exception:
            pass

    if not base and hasattr(client, "webdav"):
        try:
            base = (getattr(client.webdav, "hostname", "") or getattr(client.webdav, "webdav_hostname", "")).rstrip("/")
            root = (getattr(client.webdav, "root", "") or getattr(client.webdav, "webdav_root", "")).strip("/")
        except Exception:
            pass

    if not base:
        base = (getattr(client, "hostname", "") or getattr(client, "webdav_hostname", "")).rstrip("/")

    if not base:
        raise RuntimeError("Cannot resolve WebDAV base URL from client (no options/webdav/hostname).")

    return base, root

def _build_full_url(client, remote_path: str) -> str:
    base, root = _resolve_webdav_base_and_root(client)
    joined_path = "/".join(p for p in [root, remote_path.lstrip("/")] if p)
    # кодируем по сегментам, чтобы пробелы/кириллица были верно процитированы
    quoted_path = "/".join(quote(seg, safe="") for seg in joined_path.split("/"))
    return f"{base}/{quoted_path}"

def _resolve_auth(client):
    """
    Возвращает (auth_obj | None). Сначала пробуем session.auth,
    иначе собираем из client.webdav/options (Basic или Digest).
    """
    from requests.auth import HTTPBasicAuth, HTTPDigestAuth

    sess = getattr(client, "session", None)
    if sess is not None and getattr(sess, "auth", None):
        return sess.auth

    login = password = None
    auth_type = None

    if hasattr(client, "webdav"):
        login = getattr(client.webdav, "login", None) or getattr(client.webdav, "user", None)
        password = getattr(client.webdav, "password", None)
        auth_type = (getattr(client.webdav, "auth", None) or "").lower()

    if (login is None or password is None) and hasattr(client, "options"):
        opt = client.options
        login = login or opt.get("webdav_login")
        password = password or opt.get("webdav_password")
        auth_type = (auth_type or opt.get("webdav_auth_type") or "").lower()

    if not login or not password:
        return None  # надеемся на already-configured sess (но в твоём случае это и было проблемой)

    if "digest" in (auth_type or ""):
        return HTTPDigestAuth(login, password)
    # по умолчанию — Basic
    return HTTPBasicAuth(login, password)

def _download_file_safe(client, remote_path: str, local_path: str) -> bool:
    """
    Сначала стандартный download_sync.
    При KeyError('content-length') — raw GET через client.session с явной auth.
    """
    full_url = _build_full_url(client, remote_path)
    sess = getattr(client, "session", None)
    if sess is None:
        raise RuntimeError("WebDAV client has no 'session' to perform raw GET fallback")

    auth = _resolve_auth(client)  # ← ключевое: даём креды явно
    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    with sess.get(full_url, stream=True, allow_redirects=True, auth=auth) as resp:
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return True


# === cloud_uploader.py (заменить реализацию append_report_line_to_cloud) ===
def _download_bytes_safe(client, remote_path: str) -> bytes | None:
    """
    Аккуратно скачивает файл в память. Возвращает bytes или None (если 404/нет файла).
    """
    try:
        full_url = _build_full_url(client, remote_path)
        auth = _resolve_auth(client)
        sess = getattr(client, "session", None)
        if sess is None:
            import requests as _req
            sess = _req.Session()
        resp = sess.get(full_url, auth=auth, stream=True)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content or b""
    except Exception as e:
        logger.warning(f"[REPORTS] download {remote_path} failed: {e}")
        return None

def append_report_line_to_cloud(remote_folder_path: str, created_start_time: str, created_end_time: str, file_name: str,
                                report_filename: str = "reports.txt") -> bool:
    """
    Без временных файлов:
    - GET в память (если нет файла — стартуем с пустого контента)
    - append строки (c \n при необходимости)
    - PUT обратно
    """
    try:
        if not create_folder_if_not_exists(client, remote_folder_path):
            return False

        remote_file_path = posixpath.join(remote_folder_path, report_filename)

        # 1) Проверяем существование
        try:
            exists = client.check(remote_file_path)
        except Exception:
            exists = False

        # 2) Сформируем строку
        line = f"{created_start_time} - {created_end_time}   {file_name}"

        # 3) Считываем текущее содержимое (если есть) в память
        content = b""
        if exists:
            data = _download_bytes_safe(client, remote_file_path)
            if data is None:
                # трактуем как «нет файла»
                exists = False
            else:
                content = data

        # 4) Акт аккуратной дописки \n
        needs_nl = len(content) > 0 and not content.endswith(b"\n")
        to_upload = content + (b"\n" if needs_nl else b"") + line.encode("utf-8")

        # 5) Заливаем обратно одной операцией
        ok = upload_bytes_to_cloud(client, to_upload, remote_file_path, content_type="text/plain; charset=utf-8")
        if ok:
            logger.info(f"[REPORTS] Обновлён {remote_file_path}")
            invalidate_folder_now(remote_folder_path, meta_cache)
            invalidate_path_now(remote_file_path, meta_cache)
        return ok

    except Exception as e:
        logger.error(f"[REPORTS] Не удалось обновить {remote_folder_path}/{report_filename}: {e}\n{traceback.format_exc()}")
        return False


async def check_if_interest_video_exists(interest_name: str) -> bool:
    """
    Проверяет наличие видео (.mp4) в папке интереса.
    Если конкретный файл не найден, то ищет любой *.mp4 в папке.
    """
    try:
        # 1. Получаем путь до папки интереса
        _, _, interest_folder_path = get_interest_folder_path(interest_name, dest_directory=settings.CLOUD_PATH)

        # 3. Если не найден — ищем любой mp4-файл в этой папке
        items = await cached_list(client, interest_folder_path)
        for x in items:
            if x.lower().endswith(".mp4"):
                return True
        return False

    except Exception as e:
        logger.warning(f"[check_if_interest_video_exists] Ошибка проверки {interest_name}: {e}")
        return False



async def _frame_exists_cloud_async(folder: str, channel_id: int) -> bool:
    try:
        items = await cached_list(client, folder)
        base = {posixpath.basename(x.rstrip("/")) for x in items}
        for suffix in (f"ch{channel_id}_first.jpg", f"ch{channel_id}_last.jpg"):
            if any(name.endswith(suffix) for name in base):
                return True
        # fallback: точечные check (тоже через кэш)
        for suffix in (f"ch{channel_id}_first.jpg", f"ch{channel_id}_last.jpg"):
            path = posixpath.join(folder, suffix)
            if await cached_check(client, path):
                return True
        return False
    except Exception:
        # в случае ошибки не кэшируем False, пусть повторит
        print("ERROR", traceback.format_exc())
        return False


async def ainvalidate_folder(folder: str, meta_cache) -> None:
    """
    Асинхронно и детерминированно инвалидирует кэш листинга и проверок существования для папки.
    Должна вызываться только из async-кода: await ainvalidate_folder(...).
    """
    base = folder.rstrip('/') + '/'
    list_prefix = f"dav:list:{base}"
    check_prefix = f"dav:check:{base}"
    # гарантированно дождёмся очистки кэша
    await meta_cache.invalidate_prefix(list_prefix)
    await meta_cache.invalidate_prefix(check_prefix)

async def ainvalidate_path(path: str, meta_cache) -> None:
    await meta_cache.invalidate(_cache_key_check(path))

def invalidate_path_now(path: str, meta_cache) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(ainvalidate_path(path, meta_cache))
    else:
        raise RuntimeError("invalidate_path_now() вызвана внутри активного event loop. "
                           "В async-коде используйте: await ainvalidate_path(...).")

def invalidate_folder_now(folder: str, meta_cache) -> None:
    """
    Синхронная версия: блокирующе *дожидается* очистки кэша.
    Нельзя вызывать внутри уже работающего event loop (внутри async-кода).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # цикла нет — безопасно выполнить до конца
        asyncio.run(ainvalidate_folder(folder, meta_cache))
    else:
        # мы внутри уже запущенного event loop — в sync-функции блокироваться нельзя
        # чтобы избежать дедлоков/предсказуемо упасть:
        raise RuntimeError("invalidate_folder_now() вызвана внутри активного event loop. "
                           "В async-коде используйте: await ainvalidate_folder(...).")


async def acreate_folder_if_not_exists(client, folder_path: str) -> bool:
    """
    Async-версия создания папки, безопасная для вызова внутри event loop.
    Внутри все блокирующие вызовы уводит в thread, а инвалидацию кэша делает через await.
    """
    try:
        # check() тоже иногда может падать/таймаутиться — даём несколько попыток
        check_attempt = 1
        exists = False
        while check_attempt <= 3:
            try:
                exists = await asyncio.to_thread(client.check, folder_path)
                break
            except Exception as e:
                logger.warning(
                    f"Ошибка при проверке существования папки {folder_path}: {e} "
                    f"Попытка {check_attempt}/3"
                )
                if check_attempt == 3:
                    logger.info(f"{folder_path}: пробуем создать папку несмотря на ошибки check()")
                else:
                    await asyncio.sleep(1)
                check_attempt += 1

        if exists:
            return True

        logger.info(f"Папка {folder_path} не существует. Создаю...")
        create_attempt = 1
        while create_attempt <= 2:
            try:
                # mkdir — блокирующий вызов webdav3 → в thread
                await asyncio.to_thread(client.mkdir, folder_path)

                # инвалидация кэша — async-варианты
                parent = posixpath.dirname(folder_path)
                await ainvalidate_folder(parent, meta_cache)
                await ainvalidate_path(folder_path, meta_cache)
                return True
            except Exception as e:
                logger.warning(
                    f"Ошибка при создании папки {folder_path} на WebDAV! ({e}) "
                    f"Попытка {create_attempt}/2"
                )
                create_attempt += 1
                await asyncio.sleep(1)

        logger.critical(f"Не удалось создать папку {folder_path}")
        return False

    except Exception as e:
        logger.error(f"Ошибка при проверке/создании папки {folder_path}: {e}")
        return False


def create_folder_if_not_exists(client, folder_path):
    """
    Проверяем существование папки и создаем её, если она отсутствует.
    """
    try:
        check_attempt = 1
        exists = False
        while check_attempt <= 3:
            try:
                exists = client.check(folder_path)
                break
            except Exception as e:
                logger.warning(
                    f"Ошибка при проверке существования папки {folder_path}: {e} "
                    f"Попытка {check_attempt}/3"
                )
                if check_attempt == 3:
                    logger.info(f"{folder_path}: пробуем создать папку несмотря на ошибки check()")
                else:
                    time.sleep(1)
                check_attempt += 1

        if exists:
            return True  # Уже есть
        logger.info(f"Папка {folder_path} не существует. Создаю...")
        count = 0
        while count < 2:
            try:
                client.mkdir(folder_path)
                invalidate_folder_now(posixpath.dirname(folder_path), meta_cache)
                invalidate_path_now(folder_path, meta_cache)
                return True
            except Exception as e:
                logger.warning(
                    f"Ошибка при создании папки {folder_path} на WebDAV! ({e}) "
                    f"Попытка {count+1}/2")
                count += 1
                time.sleep(1)
        logger.critical(f"Не удалось создать папку {folder_path}")
        return False
    except Exception as e:
        logger.error(f"Ошибка при проверке существования папки {folder_path}: {e}")
        return False


def upload_file_to_cloud(client, local_file_path, remote_path, retries=4, base_delay=0.8):
    """
    Загрузка файла на WebDAV сервер в указанную папку с "слипучими" повторами.
    Экспоненциальный backoff + jitter:
      попытки: 1..retries, задержка = base_delay * (2**(attempt-1)) + rand[0..0.3]
    Возвращает True при успехе, иначе False.
    """
    for attempt in range(1, retries + 1):
        try:
            client.upload_sync(remote_path=remote_path, local_path=local_file_path)
            logger.info(f"Файл {local_file_path} → {remote_path}: OK")
            invalidate_folder_now(posixpath.dirname(remote_path), meta_cache)
            invalidate_path_now(remote_path, meta_cache)
            return True
        except Exception as e:
            # 4xx (кроме 429) смысла ретраить мало; webdav3, увы, не всегда даёт код.
            # Поэтому просто делаем несколько попыток с растущей задержкой.
            logger.warning(f"Upload fail {attempt}/{retries} for {local_file_path} → {remote_path}: {e}")
            if attempt == retries:
                logger.error(f"Файл {local_file_path} не удалось залить после {retries} попыток.")
                return False
            # экспонента + небольшой джиттер
            sleep_for = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
            time.sleep(sleep_for)


def delete_local_file(local_file_path):
    """
    Удаление локального файла после успешной загрузки.
    """
    try:
        os.remove(local_file_path)
        logger.debug(f"Локальный файл {local_file_path} удалён.")
    except OSError as e:
        logger.debug(f"Не удалось удалить локальный файл {local_file_path}: {e}")


def get_interest_folder_path(interest_name, dest_directory):
    plate, date_str, _, _ = functions.parse_interest_name(interest_name)
    registr_folder = posixpath.join(dest_directory, plate)
    date_folder_path = posixpath.join(registr_folder, date_str)
    interest_folder_path = posixpath.join(
        date_folder_path, os.path.basename(interest_name).rstrip("/"))
    return registr_folder, date_folder_path, interest_folder_path


def create_interest_folder_path(interest_name, dest_directory):
    registr_folder, date_folder_path, interest_folder_path = get_interest_folder_path(
        interest_name, dest_directory)

    created_registr = create_folder_if_not_exists(client, registr_folder)
    created_date = create_folder_if_not_exists(client, date_folder_path)
    created_interest = create_folder_if_not_exists(client, interest_folder_path)

    if not (created_registr and created_date and created_interest):
        logger.error(
            f"Не удалось создать структуру папок для интереса {interest_name}. "
            f"registr_folder: {created_registr}, "
            f"date_folder_path: {created_date}, "
            f"interest_folder_path: {created_interest}")
        return None  # Явно

    return {"register_folder_path": registr_folder,
            "date_folder_path": date_folder_path,
            "interest_folder_path": interest_folder_path}

def upload_file(file_path, interest_folder_path):
    """
    Загружает файл и фотографии в облако через WebDAV.

    :param file_path: Путь к файлу для загрузки.
    :param dest_directory: Базовая директория на удаленном сервере.
    :param pics: Словарь с фотографиями (before и after).
    :return: True, если все файлы загружены успешно, иначе False.
    """

    remote_path = posixpath.join(interest_folder_path,
                                 os.path.basename(file_path))
    success = upload_file_to_cloud(client, file_path, remote_path)
    return success


async def _upload_one(photo_path, dest_folder):
    if photo_path:
        remote_path = posixpath.join(dest_folder, os.path.basename(photo_path))
        ok = await asyncio.to_thread(upload_file_to_cloud, client, photo_path, remote_path)
        if ok:
            delete_local_file(photo_path)

async def upload_pics_async(pics, destinaton_folder, concurrency=6) -> bool:
    """
    Заливает список файлов в папку и удаляет локальные файлы после успешной загрузки.
    Возвращает True, если все (или пустой список) прошли успешно.
    """
    if not pics:
        await ainvalidate_folder(destinaton_folder, meta_cache)
        return True

    sem = asyncio.Semaphore(concurrency)

    async def one(photo_path: str) -> bool:
        if not photo_path:
            return True
        remote_path = posixpath.join(destinaton_folder, os.path.basename(photo_path))
        async with sem:
            ok = await asyncio.to_thread(upload_file_to_cloud, client, photo_path, remote_path)
            if ok:
                delete_local_file(photo_path)
            return bool(ok)

    results = await asyncio.gather(*(one(p) for p in pics), return_exceptions=False)
    # после пачки — гарантированная инвалидация папки
    await ainvalidate_folder(destinaton_folder, meta_cache)
    return all(results)

async def create_pics_async(before_frames, after_frames, before_folder, after_folder) -> bool:
    ok1 = await upload_pics_async(before_frames, before_folder)
    ok2 = await upload_pics_async(after_frames, after_folder)
    return bool(ok1 and ok2)

def upload_dict_as_json_to_cloud(data: dict, remote_folder_path: str,
                                 filename: str = "report.json"):
    """
    Сохраняет словарь в JSON и загружает на WebDAV в указанную папку.

    :param data: Словарь с данными для сохранения
    :param remote_folder_path: Папка в облаке для загрузки (WebDAV)
    :param filename: Имя файла (по умолчанию — data.json)
    """
    logger.info(f"Выгрузка отчета в {remote_folder_path}")
    try:
        # Уникальное имя временного файла
        local_filename = f"{uuid.uuid4().hex}.json"
        local_file_path = os.path.join(settings.REPORTS_TEMP_FOLDER,
                                       local_filename)

        # Сохраняем словарь в JSON
        with open(local_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        # Убедимся, что папка в облаке существует
        a = create_folder_if_not_exists(client, remote_folder_path)
        if not a:
            return
        # Задаём путь в облаке
        remote_file_path = posixpath.join(remote_folder_path, filename)

        # Загружаем файл
        success = upload_file_to_cloud(client, local_file_path,
                                       remote_file_path)

        # Удаляем локальный файл после загрузки
        if success:
            delete_local_file(local_file_path)
        logger.info("Отчет успешно выгружен")
        if success:
            invalidate_folder_now(remote_folder_path, meta_cache)
            invalidate_path_now(remote_file_path, meta_cache)
        return success

    except Exception as e:
        logger.error(f"Ошибка при сохранении JSON в облако: {e}")
        return False
