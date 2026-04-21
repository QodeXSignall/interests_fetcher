from interests_fetcher.logger import logger
from interests_fetcher.data import settings
from interests_fetcher.filelocker import FileLock, _load_states, _atomic_save_states, LOCK_PATH
from interests_fetcher.truck_state import (
    ensure_truck_row_for_mdvr,
    ensure_truck_structure_inplace,
    find_truck_id_by_mdvr,
    flatten_truck_for_pipeline,
)
from typing import Iterable, Iterator, Tuple, Dict, Any, Optional
from typing import List
import subprocess
import datetime
import zipfile
import ffmpeg
import shutil
import json
import uuid
import time
import os
import re



def rename_file_on_disk(path: str, new_name: str) -> str:
    """
    Переименовывает файл по указанному пути в новое имя.
    Возвращает новый путь.
    """
    directory = os.path.dirname(path)
    new_path = os.path.join(directory, new_name)
    os.rename(path, new_path)
    return new_path



def unzip_archives_in_directory(input_dir, output_dir):
    # Проверка существования входящей директории
    logger.debug(f'Распаковка {input_dir} в {output_dir}')
    if not os.path.exists(input_dir):
        logger.error(f'Директория {input_dir} не найдена')
        return
    # Получение списка всех файлов в input_dir
    files = os.listdir(input_dir)
    for file in files:
        logger.debug(f'Распаковка файла {file}...')
        # Проверяем, является ли файл архивом .zip
        if file.endswith('.zip'):
            zip_path = os.path.join(input_dir, file)
            # Определяем имя архива без расширения
            archive_name = os.path.splitext(file)[0]
            # Формируем путь для новой директории
            new_output_dir = os.path.join(output_dir, archive_name)
            if os.path.exists(new_output_dir):
                continue
            # Создаём новую директорию, если она не существует
            if not os.path.exists(new_output_dir):
                os.makedirs(new_output_dir)
            # Распаковка архива
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(path=new_output_dir)
                logger.debug(
                    f'Файл {file} успешно распакован в {new_output_dir}.')
    logger.info(f'Распаковка {input_dir} в {output_dir} завершена.')


def split_time_range_to_dicts(start_time, end_time, interval):
    if isinstance(start_time, str): start_time = datetime.datetime.fromisoformat(start_time)
    if isinstance(end_time, str):   end_time   = datetime.datetime.fromisoformat(end_time)
    if start_time >= end_time: raise ValueError("...")
    result = []
    cur = start_time
    while cur < end_time:
        nxt = min(cur + interval, end_time)
        result.append({"time_start": cur, "time_end": nxt})
        cur = nxt
    return result


def concatenate_videos(converted_files, output_abs_name, reg_id, interest_name):
    concat_candidates = []
    if os.path.exists(output_abs_name):
        logger.info(f"[CONCAT] Видео уже было конкатенировано ранее, найдено: {output_abs_name}")
        return

    # --- Сортировка по времени начала в имени файла ---
    # Формат у тебя ...-ДДММГГ- HHMMSS - HHMMSS -....
    def extract_time_key(path: str):
        base = os.path.basename(path)
        parts = re.findall(r'-(\d{6})-', base)
        if len(parts) >= 3:
            date, start, end = parts[0], parts[1], parts[2]
            # tie-breaker: basename чтобы порядок был детерминированным при равных временах
            return (int(date), int(start), int(end), base)
        elif len(parts) >= 2:
            return (int(parts[0]), int(parts[1]), -1, base)
        elif len(parts) == 1:
            return (int(parts[0]), -1, -1, base)
        return (float('inf'), float('inf'), float('inf'), base)

    # Сначала очищаем от пустых, потом сортируем
    converted_files = [f for f in converted_files if f]
    converted_files = sorted(converted_files, key=extract_time_key)

    # --- Фильтрация существующих и непустых файлов ---
    for f in converted_files:
        try:
            if os.path.isfile(f) and os.path.getsize(f) > 0:
                concat_candidates.append(f)
            else:
                logger.error(f"{reg_id}: {interest_name} [CONCAT] Файл отсутствует или пустой: {f}")
        except OSError as e:
            logger.error(f"{reg_id}: {interest_name} [CONCAT] Ошибка доступа к файлу {f}: {e}")

    if len(concat_candidates) == 0:
        raise FileNotFoundError(f"{reg_id}: {interest_name} [CONCAT] Нет ни одного валидного входного файла — пропускаю интерес.")

    # Перестрахуемся: создадим каталог для выходного файла и списка конкатенации
    out_dir = os.path.dirname(output_abs_name)
    os.makedirs(out_dir, exist_ok=True)

    if len(concat_candidates) == 1:
        src = concat_candidates[0]
        shutil.copyfile(src, output_abs_name)
        logger.debug(f"{reg_id}: {interest_name} [CONCAT] Единственный файл — скопирован: {src} -> {output_abs_name}")
        return

    # Лог ключей именно по итоговым кандидатам
    logger.debug(f"{reg_id}: {interest_name} [CONCAT] Ключи: {[(os.path.basename(f), extract_time_key(f)) for f in concat_candidates]}")
    logger.debug(f"{reg_id}: {interest_name} [CONCAT] Конкатенация файлов {concat_candidates}")

    # Готовим список для ffmpeg concat (нормализуем слэши и экранируем одиночные кавычки)
    concat_list_path = os.path.join(out_dir, f"concat_list_{uuid.uuid4().hex}.txt")
    try:
        with open(concat_list_path, "w", encoding="utf-8", newline="\n") as f:
            for file in concat_candidates:
                norm = file.replace("\\", "/").replace("'", r"\'")
                f.write(f"file '{norm}'\n")

        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
               "-i", concat_list_path, "-c", "copy", output_abs_name]

        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.debug(f"{reg_id}: {interest_name} [CONCAT] Успех. Результат: {output_abs_name}")

    except subprocess.CalledProcessError as e:
        logger.error(f"{reg_id}: {interest_name} [CONCAT] ffmpeg упал: {e.stderr or e.stdout}")
        raise
    finally:
        try:
            os.remove(concat_list_path)
        except OSError:
            pass



def convert_video_file(input_video_path: str, output_dir: str = None,
                       output_format: str = "mp4"):
    if not output_dir:
        logger.debug("Output dir for converted files is not specified. "
                     "Input file`s dir has been choosen.")
        output_dir = os.path.dirname(input_video_path)
    filename = os.path.basename(input_video_path)
    output_video_path = os.path.join(output_dir,
                                     filename + '.' + output_format)
    # Команда для конвертации через FFMPEG
    conversion_command = ['ffmpeg', '-y', '-i', input_video_path, '-c:v',
                          'libx264', '-crf', '23', '-preset', 'medium',
                          output_video_path]
    logger.debug(
        f"Команда на конвертацию {' '.join(conversion_command)}")
    try:
        subprocess.run(conversion_command, check=True)
    except subprocess.CalledProcessError:
        logger.critical("Ошибка конвертации!")
        return None
    return output_video_path


def ensure_alarms_structure_inplace(states: dict, reg_id: str | None = None) -> bool:
    """
    НИЧЕГО НЕ ПИШЕТ В ФАЙЛ. Только правит trucks in-place (по mdvr или все).
    Возвращает True, если структура была дополнена/исправлена.
    """
    trucks = states.setdefault("trucks", {})
    changed = False
    if reg_id is None:
        for tid in list(trucks.keys()):
            if ensure_truck_structure_inplace(trucks[tid]):
                changed = True
        return changed
    tid = find_truck_id_by_mdvr(states, reg_id)
    if not tid:
        return False
    return ensure_truck_structure_inplace(trucks[tid])


def save_new_interests(reg_id, interests):
    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        states["trucks"][tid]["interests"] = interests  # <-- тут был баг: раньше писалось в "states"
        _atomic_save_states(states)

def _get_processed_set(reg_id: str) -> set[str]:
    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        reg = states["trucks"][tid]
        processed = reg.get("processed_interests", [])
        return set(processed)

def _save_processed(reg_id: str, name: str, keep_last: int = 1000):
    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        reg = states["trucks"][tid]
        arr = reg.get("processed_interests", [])
        if name not in arr:
            arr.append(name)
            # ограничим размер кольцевым буфером
            if len(arr) > keep_last:
                arr = arr[-keep_last:]
            reg["processed_interests"] = arr
        _atomic_save_states(states)

def filter_already_processed(reg_id: str, interests: list[dict]) -> list[dict]:
    done = _get_processed_set(reg_id)
    out = []
    for it in interests:
        nm = it.get("name")
        if nm in done:
            logger.info(f"[DEDUP] Пропуск уже обработанного интереса: {nm}")
            continue
        out.append(it)
    return out

def clean_interests(reg_id):
    with FileLock(LOCK_PATH):
        logger.debug("Cleaning interests in states.json")
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        states["trucks"][tid]["interests"] = []
        _atomic_save_states(states)



def get_reg_info(reg_id: str):
    with FileLock(LOCK_PATH):
        states = _load_states()
        _, _, created = ensure_truck_row_for_mdvr(states, reg_id)
        changed = ensure_alarms_structure_inplace(states, reg_id) or created
        if changed:
            _atomic_save_states(states)
        tid = find_truck_id_by_mdvr(states, reg_id)
        return flatten_truck_for_pipeline(states["trucks"][tid])



def create_new_reg(reg_id, plate):
    with FileLock(LOCK_PATH):
        states = _load_states()
        tid, row, created = ensure_truck_row_for_mdvr(states, reg_id, plate=plate)
        if not created:
            return flatten_truck_for_pipeline(row)
        ensure_alarms_structure_inplace(states, reg_id)
        _atomic_save_states(states)
        return flatten_truck_for_pipeline(states["trucks"][tid])


def save_new_reg_last_upload_time(reg_id: str, timestamp: str):
    try:
        new_dt = datetime.datetime.strptime(timestamp, settings.TIME_FMT)
    except Exception:
        logger.warning(f"{reg_id}. Некорректный формат last_upload_time: {timestamp} — игнор.")
        return

    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        reg = states["trucks"][tid]

        cur_str = reg.get("last_upload_time")
        cur_dt = None
        if cur_str:
            try:
                cur_dt = datetime.datetime.strptime(cur_str, settings.TIME_FMT)
            except Exception:
                pass

        if cur_dt is None or new_dt > cur_dt:
            reg["last_upload_time"] = timestamp
            _atomic_save_states(states)
            logger.info(f"{reg_id}. Обновлен `last_upload_time`: {timestamp}")
        else:
            logger.debug(f"{reg_id}. Пропуск обновления last_upload_time (новое {timestamp} <= текущее {cur_str}).")


def _interest_name_to_interval(name: str) -> tuple[str, datetime.datetime, datetime.datetime]:
    """
    A939CA702_2025.11.23 07.10.16-07.14.19  ->  (plate, start_dt, end_dt)
    """
    plate, date_str, start_str, end_str = parse_interest_name(name)
    # Формат имени интереса: YYYY.MM.DD HH.MM.SS
    interest_fmt = "%Y.%m.%d %H.%M.%S"

    start_dt = datetime.datetime.strptime(f"{date_str} {start_str}", interest_fmt)
    end_dt   = datetime.datetime.strptime(f"{date_str} {end_str}",   interest_fmt)

    return plate, start_dt, end_dt


def exact_diff_sets(expected: set[str], detected: set[str]) -> tuple[set[str], set[str]]:
    """
    exact comparison:
      expected — имена, которые уже есть на WebDAV
      detected — имена, которые новый алгоритм видит сейчас

    Возвращает:
      new_exact     — новые интересы (detected - expected)
      missing_exact — лишние интересы (expected - detected)
    """
    new_exact = detected - expected
    missing_exact = expected - detected
    return new_exact, missing_exact


def video_remover_cycle():
    while True:
        all_videos = get_all_files(settings.INTERESTING_VIDEOS_FOLDER)
        for video_abs_name in all_videos:
            if check_if_file_old(video_abs_name):
                os.remove(video_abs_name)
        time.sleep(3600)


def get_all_files(files_dir):
    only_files = [os.path.join(files_dir, f) for f in os.listdir(files_dir)
                  if os.path.isfile(os.path.join(files_dir, f))]
    return only_files


def check_if_file_old(file_abs_path, old_time_days=60):
    ti_m = os.path.getmtime(file_abs_path)
    created_time = datetime.datetime.fromtimestamp(ti_m)
    if (datetime.datetime.now() - created_time).days >= old_time_days:
        return True


def get_video_info(file_path):
    """
    Получает информацию о видеофайле: формат и видеокодек.
    """
    try:
        probe = ffmpeg.probe(file_path)
        print(probe)
        format_name = probe['format']['format_name']
        video_stream = next((stream for stream in probe['streams'] if
                             stream['codec_type'] == 'video'), None)
        video_codec = video_stream['codec_name'] if video_stream else None
        return format_name, video_codec
    except ffmpeg.Error as e:
        logger.error(f"Ошибка при анализе файла {file_path}: {e.stderr}")
        return None, None


def convert_to_mp4_h264(input_file, output_file):
    """
    Конвертирует видео в MP4 с кодеком H.264.
    1) MP4, H.264 → копируем без изменений
    2) IFV-файл (метаданные битые) → обрабатываем как H.265, FPS=5
    3) MP4, H.265 → перекодируем в H.264
    """
    try:
        input_ext = os.path.splitext(input_file)[-1].lower()

        # 1) Если файл уже MP4 с H.264, просто копируем
        if input_ext == ".mp4":
            video_codec = get_video_codec(input_file)
            if video_codec == "h264":
                logger.info(
                    f"Файл {input_file} уже в формате MP4, H.264. Копируем без изменений.")
                shutil.copy(input_file, output_file)
                return

        # 2) Если файл содержит ".ifv" в названии, обрабатываем его как H.265, FPS=5
        if ".ifv" in input_file:
            logger.info(
                f"Файл {input_file} определен как IFV. Предполагаем кодек H.265, FPS=5.")

        # 3) Если кодек H.265 (HEVC) или мы обрабатывали IFV, перекодируем в H.264
        logger.info(f"Конвертируем {input_file} в MP4 (H.264).")
        ffmpeg.input(input_file, vcodec="hevc").output(output_file, vcodec="libx264", preset="medium").run(
            overwrite_output=True
        )

        logger.info(f"Файл успешно обработан: {output_file}")

    except ffmpeg.Error as e:
        logger.error(f"Ошибка при обработке файла {input_file}: {e.stderr}")


def get_video_codec(file_path):
    """
    Определяет видеокодек файла через ffmpeg.
    """
    try:
        probe = ffmpeg.probe(file_path)
        return probe["streams"][0]["codec_name"]
    except Exception as e:
        logger.warning(f"Не удалось определить кодек для {file_path}: {e}")
        return None  # Если не удалось определить, возвращаем None


def process_video_file(file_path, output_file_path):
    """
    Обрабатывает видеофайл: проверяет формат и кодек, при необходимости конвертирует.
    """
    # Получаем информацию о файле
    format_name, video_codec = get_video_info(file_path)
    if not format_name or not video_codec:
        logger.error(f"Не удалось получить информацию о файле: {file_path}")
        return file_path

    logger.info(f"Файл: {file_path}")
    logger.info(f"Формат: {format_name}")
    logger.info(f"Видеокодек: {video_codec}")

    if not settings.config.getboolean("Video", "convert_required"):
        logger.info("Конвертация отключена в конфиге, пропуск...")
        return file_path

    # Определяем, нужно ли конвертировать
    need_conversion = False
    if format_name != 'mp4':
        need_conversion = True
        logger.info(f"Файл не в формате MP4 ({format_name}). Требуется конвертация.")
    elif video_codec != 'h264':
        need_conversion = True
        logger.info(
            f"Файл в формате MP4, но кодек не H.264 ({video_codec}). Требуется конвертация.")
    else:
        logger.info(
            "Файл уже в формате MP4 с кодеком H.264. Конвертация не требуется.")

    # Конвертируем, если нужно
    if need_conversion:
        #output_file = os.path.splitext(file_path)[0] + "_converted.mp4"
        convert_to_mp4_h264(file_path, output_file_path)
        return output_file_path
    return file_path


def parse_interest_name(name: str):
    """
    Разбирает имя интереса вида:
    "<PLATE>_YYYY.MM.DD HH.MM.SS-HH.MM.SS" (опц. расширение в конце).
    Возвращает (plate, date_str, start_str, end_str).
    Бросает ValueError при несоответствии.
    """
    base = os.path.basename(name)
    m = settings._INTEREST_RE.match(base)
    if not m:
        raise ValueError(f"Invalid interest name format: {name!r}")
    gd = m.groupdict()
    return gd["plate"], gd["date"], gd["start"], gd["end"]

def build_interest_name(plate: str, date_str: str, start_str: str, end_str: str, ext: str | None = None) -> str:
    """
    Собирает имя интереса из частей:
    (plate, date_str, start_str, end_str[, ext]) ->
    "<PLATE>_YYYY.MM.DD HH.MM.SS-HH.MM.SS[.ext]"
    """
    # Базовая часть
    name = f"{plate}_{date_str} {start_str}-{end_str}"
    # Опциональное расширение (например, ".mp4" или ".zip")
    if ext:
        # убираем точку, если пользователь случайно передал ".mp4"
        ext = ext.lstrip(".")
        name = f"{name}.{ext}"
    return name

def merge_overlapping_interests(interests: List[dict]) -> List[dict]:
    if not interests:
        return []
    # Сортируем по началу
    sorted_interests = sorted(interests, key=lambda x: x['beg_sec'])
    merged = []

    current = sorted_interests[0].copy()
    for next_interest in sorted_interests[1:]:
        # Пересекаются, если начало следующего раньше конца текущего
        if next_interest['beg_sec'] <= current['end_sec']:
            logger.info(
                f"{current['reg_id']}: Обнаружение пересечение интересов {current['name']} и {next_interest['name']}. "
                f"Объединение...")
            # Объединяем интервалы
            current['beg_sec'] = min(current['beg_sec'], next_interest['beg_sec'])
            current['end_sec'] = max(current['end_sec'], next_interest['end_sec'])

            # Объединяем временные метки
            current['start_time'] = min(current['start_time'], next_interest['start_time'])
            current['end_time'] = max(current['end_time'], next_interest['end_time'])
            current['photo_before_timestamp'] = min(
                current.get('photo_before_timestamp', current['start_time']),
                next_interest.get('photo_before_timestamp', next_interest['start_time'])
            )
            current['photo_after_timestamp'] = max(
                current.get('photo_after_timestamp', current['end_time']),
                next_interest.get('photo_after_timestamp', next_interest['end_time'])
            )

            # Объединяем фото в секундах
            current['photo_before_sec'] = min(current.get('photo_before_sec', current['beg_sec']),
                                              next_interest.get('photo_before_sec', next_interest['beg_sec']))
            current['photo_after_sec'] = max(current.get('photo_after_sec', current['end_sec']),
                                             next_interest.get('photo_after_sec', next_interest['end_sec']))

            # Объединяем события переключателей
            if 'report' in current and 'report' in next_interest:
                current_switches = current['report'].get('switch_events', [])
                next_switches = next_interest['report'].get('switch_events', [])
                merged_switches = current_switches + next_switches
                merged_switches.sort(key=lambda x: x['datetime'])
                current['report']['switch_events'] = merged_switches
                current['report']['switches_amount'] = len(merged_switches)

            # Меняем имя интереса
            plate, date, start, _ = parse_interest_name(current['name'])
            _, _, _, end = parse_interest_name(next_interest['name'])
            new_name = build_interest_name(plate, date, start, end)
            current['name'] = new_name

            logger.info(f"{current['reg_id']}: Объединенный интерес - {current['name']}")
        else:
            merged.append(current)
            current = next_interest.copy()

    merged.append(current)
    return merged

def get_pending_interests(reg_id: str) -> list[dict]:
    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            # Жёсткая ситуация: в файле нет такого регистратора.
            # Мы не создаём дефолт (чтобы не потерять данные молча),
            # а возвращаем пустой список. Логируем warning.
            logger.warning(f"{reg_id}: get_pending_interests -> регистратор не найден в states.json")
            return []

        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]
        return list(reg.get("pending_interests", []))


def set_pending_interests(reg_id: str, interests: list[dict]) -> None:
    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        states["trucks"][tid]["pending_interests"] = list(interests)
        _atomic_save_states(states)

def append_pending_interests(reg_id: str, interests: list[dict]) -> None:
    if not interests:
        return
    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        reg = states["trucks"][tid]
        cur = reg.get("pending_interests", [])
        seen = {it.get("name") for it in cur if isinstance(it, dict)}
        for it in interests:
            nm = (it or {}).get("name")
            if not nm or nm in seen:
                continue
            # по умолчанию — интерес ничем не заблокирован
            if "blocking_gap_ids" not in it or not isinstance(it.get("blocking_gap_ids"), list):
                it["blocking_gap_ids"] = []
            # dead-letter-счётчики для download-clips (см. inc_pending_download_attempts)
            if not isinstance(it.get("download_attempts"), int):
                it["download_attempts"] = 0
            if "last_download_error_at" not in it:
                it["last_download_error_at"] = None
            cur.append(it)
            seen.add(nm)
        reg["pending_interests"] = cur
        _atomic_save_states(states)

def remove_pending_interest(reg_id: str, interest_name: str) -> None:
    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        reg = states["trucks"][tid]
        cur = reg.get("pending_interests", [])
        reg["pending_interests"] = [it for it in cur if it.get("name") != interest_name]
        _atomic_save_states(states)


def inc_pending_download_attempts(reg_id: str, interest_name: str) -> int:
    """
    Атомарно увеличивает download_attempts у pending_interest с данным name
    и обновляет last_download_error_at. Если запись не найдена — возвращает 0.
    """
    if not interest_name:
        return 0
    now_str = datetime.datetime.now().strftime(settings.TIME_FMT)
    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            logger.warning(
                f"{reg_id}: inc_pending_download_attempts -> регистратор не найден в states.json"
            )
            return 0
        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]
        cur = reg.get("pending_interests", []) or []
        new_value = 0
        for it in cur:
            if not isinstance(it, dict):
                continue
            if it.get("name") != interest_name:
                continue
            prev = it.get("download_attempts")
            if not isinstance(prev, int):
                prev = 0
            new_value = prev + 1
            it["download_attempts"] = new_value
            it["last_download_error_at"] = now_str
            break
        reg["pending_interests"] = cur
        _atomic_save_states(states)
        return new_value


# ---------------------------------------------------------------------------
# Gaps: детерминированная очередь "дыр в треках" для регистратора.
# Живут в states.trucks[tid].gaps: list[dict]. Формат записи:
#   {
#     "id":                   "<uuid4>",
#     "gap_start":            "YYYY-MM-DD HH:MM:SS",
#     "gap_end":              "YYYY-MM-DD HH:MM:SS",
#     "created_at":           "YYYY-MM-DD HH:MM:SS",
#     "last_checked":         "YYYY-MM-DD HH:MM:SS" | None,
#     "checked":              int,
#     "points_last_seen":     int,
#     "status":               "pending" | "closed" | "abandoned",
#     "closed_at":            "YYYY-MM-DD HH:MM:SS" | None,
#     "linked_interest_names": list[str],
#   }
# ---------------------------------------------------------------------------

def _new_gap_record(gap_start: str, gap_end: str, *, created_at: Optional[str] = None,
                    linked_interest_names: Optional[List[str]] = None) -> dict:
    now_str = created_at or datetime.datetime.now().strftime(settings.TIME_FMT)
    return {
        "id": str(uuid.uuid4()),
        "gap_start": gap_start,
        "gap_end": gap_end,
        "created_at": now_str,
        "last_checked": None,
        "checked": 0,
        "points_last_seen": 0,
        "no_progress_streak": 0,
        "status": "pending",
        "closed_at": None,
        "linked_interest_names": list(linked_interest_names or []),
    }


def _gaps_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    try:
        a_s = datetime.datetime.strptime(a_start, settings.TIME_FMT)
        a_e = datetime.datetime.strptime(a_end, settings.TIME_FMT)
        b_s = datetime.datetime.strptime(b_start, settings.TIME_FMT)
        b_e = datetime.datetime.strptime(b_end, settings.TIME_FMT)
    except Exception:
        return False
    return a_s <= b_e and b_s <= a_e


def get_gaps(reg_id: str) -> list[dict]:
    """Возвращает копию списка gap'ов регистратора (может быть пустым)."""
    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            logger.warning(f"{reg_id}: get_gaps -> регистратор не найден в states.json")
            return []
        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]
        return list(reg.get("gaps", []) or [])


def upsert_gap(
    reg_id: str,
    gap_start: str,
    gap_end: str,
    *,
    linked_interest_names: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Сохраняет/обновляет gap для регистратора.

    Правила:
      * если среди существующих `pending` gap'ов есть пересекающиеся с новым
        [gap_start, gap_end] — сливаем границы (min/max), сохраняем самый ранний
        `id`/`created_at`, остальные pending-дубликаты удаляем;
      * если пересечений нет — создаём новую запись со status="pending";
      * `closed`/`abandoned` gap'ы игнорируются при мердже (не поднимаем).

    Если передан `linked_interest_names` — он объединяется с уже сохранёнными
    через set-объединение (порядок стабилен).

    Возвращает `id` итоговой записи, либо None, если не удалось записать.
    """
    try:
        datetime.datetime.strptime(gap_start, settings.TIME_FMT)
        datetime.datetime.strptime(gap_end, settings.TIME_FMT)
    except Exception:
        logger.warning(f"{reg_id}: upsert_gap: некорректные границы [{gap_start} → {gap_end}]")
        return None

    if gap_start >= gap_end:
        logger.warning(f"{reg_id}: upsert_gap: пустой/инвертированный интервал [{gap_start} → {gap_end}]")
        return None

    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        reg = states["trucks"][tid]
        gaps: list[dict] = reg.setdefault("gaps", [])

        new_linked = list(linked_interest_names or [])

        # ищем все pending, пересекающиеся с новым интервалом
        overlapping_idx: list[int] = []
        for i, g in enumerate(gaps):
            if not isinstance(g, dict):
                continue
            if g.get("status") != "pending":
                continue
            if _gaps_overlap(gap_start, gap_end, g.get("gap_start", ""), g.get("gap_end", "")):
                overlapping_idx.append(i)

        if not overlapping_idx:
            record = _new_gap_record(gap_start, gap_end, linked_interest_names=new_linked)
            gaps.append(record)
            _atomic_save_states(states)
            logger.info(
                f"{reg_id}: gap created id={record['id']} "
                f"[{record['gap_start']} → {record['gap_end']}]"
            )
            return record["id"]

        # сливаем: выбираем "главную" запись — с самой ранней датой created_at
        main_i = min(
            overlapping_idx,
            key=lambda i: gaps[i].get("created_at") or "9999-12-31 23:59:59",
        )
        main = gaps[main_i]

        min_start = min([gap_start] + [gaps[i].get("gap_start", gap_start) for i in overlapping_idx])
        max_end = max([gap_end] + [gaps[i].get("gap_end", gap_end) for i in overlapping_idx])

        merged_linked: list[str] = []
        seen_linked: set[str] = set()
        for i in overlapping_idx:
            for nm in gaps[i].get("linked_interest_names") or []:
                if nm and nm not in seen_linked:
                    merged_linked.append(nm)
                    seen_linked.add(nm)
        for nm in new_linked:
            if nm and nm not in seen_linked:
                merged_linked.append(nm)
                seen_linked.add(nm)

        main["gap_start"] = min_start
        main["gap_end"] = max_end
        main["linked_interest_names"] = merged_linked

        # удалить прочие pending-дубликаты (идём с конца, чтобы индексы не поехали)
        for i in sorted(overlapping_idx, reverse=True):
            if i != main_i:
                del gaps[i]

        _atomic_save_states(states)
        logger.info(
            f"{reg_id}: gap merged id={main['id']} "
            f"[{main['gap_start']} → {main['gap_end']}] "
            f"(merged={len(overlapping_idx)})"
        )
        return main["id"]


def update_gap(reg_id: str, gap_id: str, **fields) -> bool:
    """Точечное обновление полей конкретного gap'а. Возвращает True если запись найдена."""
    if not fields:
        return False
    allowed = {
        "gap_start", "gap_end",
        "last_checked", "checked",
        "points_last_seen",
        "no_progress_streak",
        "status", "closed_at",
        "linked_interest_names",
    }
    bad = set(fields.keys()) - allowed
    if bad:
        logger.warning(f"{reg_id}: update_gap: запрещённые поля {bad}")
        return False

    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            return False
        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]
        gaps: list[dict] = reg.get("gaps") or []
        for g in gaps:
            if isinstance(g, dict) and g.get("id") == gap_id:
                g.update(fields)
                _atomic_save_states(states)
                return True
        return False


def remove_gap(reg_id: str, gap_id: str) -> bool:
    """Физически удаляет gap из списка. Возвращает True, если запись была."""
    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            return False
        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]
        gaps: list[dict] = reg.get("gaps") or []
        before = len(gaps)
        reg["gaps"] = [g for g in gaps if not (isinstance(g, dict) and g.get("id") == gap_id)]
        changed = len(reg["gaps"]) != before
        if changed:
            _atomic_save_states(states)
        return changed


def close_gap(reg_id: str, gap_id: str, *, abandoned: bool = False) -> bool:
    """
    Переводит gap в terminal status ("closed" или "abandoned"), проставляет
    closed_at=now и снимает gap_id из blocking_gap_ids всех pending_interests
    этого регистратора.
    """
    now_str = datetime.datetime.now().strftime(settings.TIME_FMT)
    new_status = "abandoned" if abandoned else "closed"

    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            return False
        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]

        target = None
        for g in reg.get("gaps") or []:
            if isinstance(g, dict) and g.get("id") == gap_id:
                target = g
                break
        if target is None:
            return False

        target["status"] = new_status
        target["closed_at"] = now_str

        for it in reg.get("pending_interests") or []:
            if not isinstance(it, dict):
                continue
            bl = it.get("blocking_gap_ids") or []
            if gap_id in bl:
                it["blocking_gap_ids"] = [x for x in bl if x != gap_id]

        _atomic_save_states(states)
        logger.info(f"{reg_id}: gap id={gap_id} -> {new_status}")
        return True


def set_blocking_gap_ids(reg_id: str, mapping: Dict[str, List[str]]) -> None:
    """
    Массово выставляет blocking_gap_ids для pending_interests указанного
    регистратора. Интересы, не упомянутые в mapping, не трогаем.
    """
    if not mapping:
        return
    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            return
        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]
        changed = False
        for it in reg.get("pending_interests") or []:
            if not isinstance(it, dict):
                continue
            nm = it.get("name")
            if nm in mapping:
                new_ids = list(mapping[nm])
                if it.get("blocking_gap_ids") != new_ids:
                    it["blocking_gap_ids"] = new_ids
                    changed = True
        if changed:
            _atomic_save_states(states)


def gc_gaps(
    reg_id: str,
    *,
    ttl_closed_days: int = 7,
    ttl_abandoned_days: int = 30,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, int]:
    """
    Физически удаляет из states.gaps регистратора все записи в terminal-статусе
    (`closed` / `abandoned`), у которых `closed_at` старше заданного TTL.

    Записи без корректного `closed_at` трогать не будем (безопасный выбор;
    в нормальном потоке close_gap всегда проставляет closed_at=now).

    Возвращает словарь:
        {"closed": N, "abandoned": M}
    — сколько записей каждого типа было удалено.
    """
    if now is None:
        now = datetime.datetime.now()
    ttl_closed = datetime.timedelta(days=max(0, int(ttl_closed_days)))
    ttl_abnd = datetime.timedelta(days=max(0, int(ttl_abandoned_days)))

    removed = {"closed": 0, "abandoned": 0}

    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            return removed
        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]
        gaps: List[dict] = reg.get("gaps") or []
        if not gaps:
            return removed

        kept: List[dict] = []
        for g in gaps:
            if not isinstance(g, dict):
                continue
            status = g.get("status")
            if status not in ("closed", "abandoned"):
                kept.append(g)
                continue
            closed_at = g.get("closed_at")
            if not closed_at:
                kept.append(g)
                continue
            try:
                ca_dt = datetime.datetime.strptime(closed_at, settings.TIME_FMT)
            except Exception:
                kept.append(g)
                continue
            ttl = ttl_closed if status == "closed" else ttl_abnd
            if (now - ca_dt) >= ttl:
                removed[status] += 1
                continue
            kept.append(g)

        if removed["closed"] or removed["abandoned"]:
            reg["gaps"] = kept
            _atomic_save_states(states)
            logger.info(
                f"{reg_id}: gc_gaps removed closed={removed['closed']}, "
                f"abandoned={removed['abandoned']}, kept={len(kept)}"
            )
    return removed


def gc_gaps_all(
    *,
    ttl_closed_days: int = 7,
    ttl_abandoned_days: int = 30,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, int]:
    """
    Пробегает по всем регистраторам в states.json и чистит терминальные gap'ы
    по TTL. Возвращает агрегированную статистику удалений.
    """
    if now is None:
        now = datetime.datetime.now()
    total = {"closed": 0, "abandoned": 0, "regs": 0}

    with FileLock(LOCK_PATH):
        states = _load_states()
        trucks = states.get("trucks") or {}
        reg_ids = []
        for tid, reg in trucks.items():
            if not isinstance(reg, dict):
                continue
            mdvr = reg.get("mdvr_serial")
            if mdvr:
                reg_ids.append(mdvr)

    for rid in reg_ids:
        r = gc_gaps(
            rid,
            ttl_closed_days=ttl_closed_days,
            ttl_abandoned_days=ttl_abandoned_days,
            now=now,
        )
        total["closed"] += r.get("closed", 0)
        total["abandoned"] += r.get("abandoned", 0)
        if r.get("closed", 0) or r.get("abandoned", 0):
            total["regs"] += 1

    if total["closed"] or total["abandoned"]:
        logger.info(
            f"[gc_gaps_all] удалено closed={total['closed']}, "
            f"abandoned={total['abandoned']} в {total['regs']} регистратор(ах)"
        )
    return total


def reconcile_gaps_in_window(
    reg_id: str,
    detected_gaps: List[Dict[str, str]],
    window_start: str,
    window_end: str,
    *,
    points_per_gap: Optional[Dict[str, int]] = None,
    stable_checks_threshold: int = 3,
    now_str: Optional[str] = None,
) -> Dict[str, List[str]]:
    """
    Сверяет `detected_gaps` (результат нового прогоняя `detect_gaps` по свежим
    трекам в окне [window_start, window_end]) с текущими pending gap'ами
    регистратора, которые ПОЛНОСТЬЮ лежат внутри этого окна.

    Применяются правила:
      * CLOSED — старый gap не пересекается ни с одним detected -> status=closed.
      * NO CHANGE — одно пересечение, границы совпадают: checked++,
        last_checked=now. Если `points_last_seen` не вырос —
        `no_progress_streak++`; иначе — 0. При достижении
        `stable_checks_threshold` -> абандон НЕ происходит здесь (решает
        `_process_gaps_if_due`, чтобы перед abandonment дернуть alarm-fallback
        из Этапа 2.5).
      * SHRUNK/EXPANDED — одно пересечение с иными границами: обновляем
        границы, checked=0, no_progress_streak=0, last_checked=now,
        points_last_seen=points_per_gap.
      * SPLIT — несколько пересечений: первое обновляет исходный gap (как
        SHRUNK), остальные — НОВЫЕ pending записи с `created_at` = created_at
        исходного gap'а.
      * NEW — detected gap, не пересекающийся ни с одним старым в окне:
        создаётся новая pending запись с `created_at=now`.

    Для CLOSED автоматически снимается `gap_id` из `blocking_gap_ids` всех
    pending_interests этого регистратора.

    Возвращает словарь:
        {
            "closed": [gap_id, ...],        # переведены в closed
            "no_progress": [gap_id, ...],   # у кого streak >= threshold
                                            # (нужны caller'у для fallback+abandon)
            "updated": [gap_id, ...],       # все gap_id, реально изменённые/созданные
        }
    """
    if now_str is None:
        now_str = datetime.datetime.now().strftime(settings.TIME_FMT)

    try:
        w_start_dt = datetime.datetime.strptime(window_start, settings.TIME_FMT)
        w_end_dt = datetime.datetime.strptime(window_end, settings.TIME_FMT)
    except Exception:
        logger.warning(f"{reg_id}: reconcile_gaps_in_window: плохое окно [{window_start} → {window_end}]")
        return {"closed": [], "no_progress": [], "updated": []}

    pts = points_per_gap or {}

    result = {"closed": [], "no_progress": [], "updated": []}

    with FileLock(LOCK_PATH):
        states = _load_states()
        tid = find_truck_id_by_mdvr(states, reg_id)
        if tid is None:
            return result
        ensure_alarms_structure_inplace(states, reg_id)
        reg = states["trucks"][tid]
        gaps: List[dict] = reg.setdefault("gaps", [])

        old_in_window: List[dict] = []
        for g in gaps:
            if not isinstance(g, dict):
                continue
            if g.get("status") != "pending":
                continue
            try:
                gs = datetime.datetime.strptime(g.get("gap_start", ""), settings.TIME_FMT)
                ge = datetime.datetime.strptime(g.get("gap_end", ""), settings.TIME_FMT)
            except Exception:
                continue
            if gs >= w_start_dt and ge <= w_end_dt:
                old_in_window.append(g)

        detected_used: set[int] = set()
        new_gaps_to_add: List[dict] = []

        for g_old in old_in_window:
            gs_old = g_old.get("gap_start", "")
            ge_old = g_old.get("gap_end", "")
            overlaps: List[tuple[int, Dict[str, str]]] = []
            for i, d in enumerate(detected_gaps):
                if i in detected_used:
                    continue
                if _gaps_overlap(gs_old, ge_old, d.get("gap_start", ""), d.get("gap_end", "")):
                    overlaps.append((i, d))

            gid = g_old.get("id") or ""
            new_points = int(pts.get(gid, 0))

            if not overlaps:
                g_old["status"] = "closed"
                g_old["closed_at"] = now_str
                result["closed"].append(gid)
                result["updated"].append(gid)
                continue

            if len(overlaps) == 1:
                i, d = overlaps[0]
                detected_used.add(i)
                d_s = d.get("gap_start", "")
                d_e = d.get("gap_end", "")
                if d_s == gs_old and d_e == ge_old:
                    prev_points = int(g_old.get("points_last_seen") or 0)
                    g_old["checked"] = int(g_old.get("checked") or 0) + 1
                    g_old["last_checked"] = now_str
                    g_old["points_last_seen"] = new_points
                    if new_points <= prev_points:
                        g_old["no_progress_streak"] = int(g_old.get("no_progress_streak") or 0) + 1
                    else:
                        g_old["no_progress_streak"] = 0
                    result["updated"].append(gid)
                    if g_old["no_progress_streak"] >= stable_checks_threshold:
                        result["no_progress"].append(gid)
                else:
                    g_old["gap_start"] = d_s
                    g_old["gap_end"] = d_e
                    g_old["checked"] = 0
                    g_old["no_progress_streak"] = 0
                    g_old["last_checked"] = now_str
                    g_old["points_last_seen"] = new_points
                    result["updated"].append(gid)
                continue

            i0, d0 = overlaps[0]
            detected_used.add(i0)
            g_old["gap_start"] = d0.get("gap_start", gs_old)
            g_old["gap_end"] = d0.get("gap_end", ge_old)
            g_old["checked"] = 0
            g_old["no_progress_streak"] = 0
            g_old["last_checked"] = now_str
            g_old["points_last_seen"] = new_points
            result["updated"].append(gid)
            for (i, d) in overlaps[1:]:
                detected_used.add(i)
                new_g = _new_gap_record(
                    d.get("gap_start", ""),
                    d.get("gap_end", ""),
                    created_at=g_old.get("created_at"),
                )
                new_gaps_to_add.append(new_g)
                result["updated"].append(new_g["id"])

        for i, d in enumerate(detected_gaps):
            if i in detected_used:
                continue
            new_g = _new_gap_record(d.get("gap_start", ""), d.get("gap_end", ""))
            new_gaps_to_add.append(new_g)
            result["updated"].append(new_g["id"])

        if new_gaps_to_add:
            gaps.extend(new_gaps_to_add)

        if result["closed"]:
            closed_set = set(result["closed"])
            for it in reg.get("pending_interests") or []:
                if not isinstance(it, dict):
                    continue
                bl = it.get("blocking_gap_ids") or []
                if bl and any(x in closed_set for x in bl):
                    it["blocking_gap_ids"] = [x for x in bl if x not in closed_set]

        _atomic_save_states(states)

    if result["closed"]:
        logger.info(
            f"{reg_id}: reconcile_gaps_in_window [{window_start} → {window_end}]: "
            f"closed={len(result['closed'])}, no_progress={len(result['no_progress'])}, "
            f"updated={len(result['updated'])}"
        )
    elif result["updated"]:
        logger.info(
            f"{reg_id}: reconcile_gaps_in_window [{window_start} → {window_end}]: "
            f"updated={len(result['updated'])}, no_progress={len(result['no_progress'])}"
        )
    return result


def _dt(x: str | datetime.datetime) -> datetime:
    return x if isinstance(x, datetime.datetime) else datetime.datetime.strptime(x, settings.TIME_FMT)

def _fmt(x: datetime.datetime) -> str:
    return x.strftime(settings.TIME_FMT)

def stitch_initial_short_gap_and_decide_fallback(
    *,
    switch_time: str | datetime.datetime,
    tracks: Iterable[Dict[str, Any]],
    # имена полей времени в треках
    begin_key: str = "beginTime",
    end_key: str = "endTime",
    # пороги
    early_window_s: int = 10,       # «мы ещё не ушли дальше 10 секунд»
    short_gap_s: int = 60,          # «короткий разрыв» ≤ 60 сек
    fallback_shift_s: int = 60,     # fallback: switch_time - 60 сек
    logger=None,
) -> Tuple[datetime.datetime, bool, Iterator[Tuple[datetime.datetime, datetime.datetime, Dict[str, Any]]]]:
    """
    Возвращает:
      - effective_start: datetime — какое время считать началом для анализа (возможно, switch_time - 60с при фолбэке)
      - fallback_used: bool — был ли применён fallback
      - segments_iter: Iterator[(seg_start, seg_end, raw_track)] — итератор по сегментам для дальнейшей обработки
        (в начале «короткий» разрыв будет сшит логически, т.е. мы просто продолжим после разрыва, не останавливаясь)

    ЛОГИКА:
      - Ищем трек, накрывающий switch_time, либо ближайший следующий.
      - Идём вперёд, отслеживаем первый разрыв между соседними треками.
      - Если разрыв встретился, а покрытие от switch_time ещё < early_window_s:
          - gap <= short_gap_s   -> шьём: игнорируем разрыв и продолжаем (segments_iter просто продолжится)
          - gap > short_gap_s    -> fallback: вернуть (switch_time - fallback_shift_s, True, ...)
      - Если разрыв впервые встретился ПОСЛЕ того, как покрытие от switch_time превысило early_window_s,
        то работаем как обычно (ничего не шьём и не делаем fallback).
    """
    sw = _dt(switch_time)
    # Отсортируем треки по началу
    tracks_sorted = sorted(
        tracks,
        key=lambda t: _dt(t[begin_key])
    )

    # Соберём список (start,end,raw)
    segs: list[Tuple[datetime.datetime, datetime.datetime, Dict[str, Any]]] = []
    for t in tracks_sorted:
        try:
            b = _dt(t[begin_key])
            e = _dt(t[end_key])
        except Exception:
            # пропускаем битые
            continue
        if e <= b:
            continue
        segs.append((b, e, t))

    # Найдём первый сегмент, который либо перекрывает switch_time, либо начинается после него
    start_idx: Optional[int] = None
    for i, (b, e, _) in enumerate(segs):
        if e >= sw:
            start_idx = i
            break
    if start_idx is None:
        # нет сегментов после switch_time — fallback сразу
        if logger:
            logger.warning("[INTEREST] Нет треков после switch_time=%s -> fallback to switch_time-60s", _fmt(sw))
        return sw - datetime.timedelta(seconds=fallback_shift_s), True, iter([])

    # Будем итерироваться, измеряя "накрытое" покрытие от sw
    covered_since_sw = 0.0
    fallback_used = False
    effective_start = sw

    # Ленивая генерация сегментов (причём «короткий» разрыв в начале просто игнорируем)
    def _iter_segments() -> Iterator[Tuple[datetime.datetime, datetime.datetime, Dict[str, Any]]]:
        nonlocal covered_since_sw, fallback_used, effective_start

        prev_end: Optional[datetime] = None
        first_segment_seen = False

        for j in range(start_idx, len(segs)):
            b, e, raw = segs[j]

            # Если первый сегмент начинается ДО switch_time — обрежем его слева
            if not first_segment_seen:
                first_segment_seen = True
                if e <= sw:
                    # теоретически не должно случиться из-за выбора start_idx, но оставим защиту
                    continue
                if b < sw:
                    # начинаем со switch_time
                    b_eff = sw
                else:
                    b_eff = b
                prev_end = b_eff  # для корректного вычисления gap на следующем шаге
                # Выдадим (b_eff, e) как первый сегмент
                yield (b_eff, e, raw)
                covered_since_sw += (e - b_eff).total_seconds()
                prev_end = e
                continue

            # Для остальных сегментов: проверяем разрыв от prev_end до b
            if prev_end is None:
                prev_end = b

            gap = (b - prev_end).total_seconds()

            if gap > 0:
                # Разрыв обнаружен
                if covered_since_sw < early_window_s:
                    # мы ещё не ушли дальше 10 секунд от switch_time
                    if gap <= short_gap_s:
                        # короткий разрыв — игнорируем, просто продолжаем
                        if logger:
                            logger.debug(
                                "[INTEREST] Короткий разрыв=%.1fs (<=%ds) в самые ранние %.1fs после switch_time — пропускаем и продолжаем.",
                                gap, short_gap_s, covered_since_sw
                            )
                        # логически «шьём» — ничего не yield-им, просто продолжаем как непрерывный поток
                        # это реализуется тем, что мы НИЧЕГО не изменяем, просто считаем b как prev_end (переход)
                        # фактически следующий сегмент начнётся с b, а prev_end был e предыдущего — «дырка» проигнорирована
                    else:
                        # длинный разрыв — fallback
                        fallback_used = True
                        effective_start = sw - datetime.timedelta(seconds=fallback_shift_s)
                        if logger:
                            logger.warning(
                                "[INTEREST] Длинный разрыв=%.1fs (>%ds) в первые %.1fs после switch_time — Fallback: start=%s",
                                gap, short_gap_s, covered_since_sw, _fmt(effective_start)
                            )
                        # Можно завершить генерацию — вызывающая сторона пересчитает логику с новым start
                        return
                # Если мы уже «ушли» дальше early_window_s — работаем как обычно, разрыв допустим

            # отдаем сегмент как есть
            yield (b, e, raw)
            covered_since_sw += (e - b).total_seconds()
            prev_end = e

    return effective_start, fallback_used, _iter_segments()
