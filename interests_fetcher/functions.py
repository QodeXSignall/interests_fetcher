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


def save_reg_verified_until(reg_id: str, timestamp: str):
    """
    Обновляет поле verified_until у регистратора.
    Логика похожа на last_upload_time:
      - парсим timestamp;
      - не даём откатываться назад.
    """
    try:
        new_dt = datetime.datetime.strptime(timestamp, settings.TIME_FMT)
    except Exception:
        logger.warning(f"{reg_id}. Некорректный формат verified_until: {timestamp} — игнор.")
        return

    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        reg = states["trucks"][tid]

        cur_str = reg.get("verified_until")
        cur_dt = None
        if cur_str:
            try:
                cur_dt = datetime.datetime.strptime(cur_str, settings.TIME_FMT)
            except Exception:
                pass

        # Разрешаем только вперёд (или на то же самое время)
        if cur_dt is None or new_dt >= cur_dt:
            reg["verified_until"] = timestamp
            _atomic_save_states(states)
            logger.info(f"{reg_id}. Обновлен `verified_until`: {timestamp}")
        else:
            logger.debug(
                f"{reg_id}. Пропуск обновления verified_until "
                f"(новое {timestamp} < текущее {cur_str})."
            )


def save_reg_verified_until_long(reg_id: str, timestamp: str):
    """
    Независимый маркер длинного recheck. Позволяем ему идти вперёд независимо от
    verified_until, чтобы окна короткой/длинной проверок не блокировали друг друга.
    """
    try:
        new_dt = datetime.datetime.strptime(timestamp, settings.TIME_FMT)
    except Exception:
        logger.warning(f"{reg_id}. Некорректный формат verified_until_long: {timestamp} — игнор.")
        return

    with FileLock(LOCK_PATH):
        states = _load_states()
        ensure_truck_row_for_mdvr(states, reg_id)
        ensure_alarms_structure_inplace(states, reg_id)
        tid = find_truck_id_by_mdvr(states, reg_id)
        reg = states["trucks"][tid]

        cur_str = reg.get("verified_until_long")
        cur_dt = None
        if cur_str:
            try:
                cur_dt = datetime.datetime.strptime(cur_str, settings.TIME_FMT)
            except Exception:
                pass

        if cur_dt is None or new_dt >= cur_dt:
            reg["verified_until_long"] = timestamp
            _atomic_save_states(states)
            logger.info(f"{reg_id}. Обновлен `verified_until_long`: {timestamp}")
        else:
            logger.debug(
                f"{reg_id}. Пропуск обновления verified_until_long "
                f"(новое {timestamp} < текущее {cur_str})."
            )


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
        # дедуп по имени интереса
        seen = {it.get("name") for it in cur if isinstance(it, dict)}
        for it in interests:
            nm = (it or {}).get("name")
            if nm and nm not in seen:
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
