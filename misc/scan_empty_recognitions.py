import json
import re
from collections import defaultdict
from typing import Dict, List, Optional
from dotenv import load_dotenv
import os
import time

BASE_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
REPORT_PATH = os.path.join(PROJECT_ROOT, "report.txt")

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
from webdav3.exceptions import RemoteResourceNotFound

from qt_pvp.data import settings
from qt_pvp import cloud_uploader, functions as main_funcs
import datetime

# Файлы отчёта распознавалки, которые пробуем искать внутри папки интереса.
# Если у тебя другое имя — просто добавь его сюда.
REPORT_CANDIDATES = [
    "qt_rm_report.json",
    "recognition_report.json",
    "recognition.json",
    "qt-rm-report.json",
    "report_qt_rm.json",
]


DAY_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")


def _append_report_line(plate: str, interest_name: str) -> None:
    """
    Добавляет строку в локальный report.txt.
    Формат: <plate>\t<interest_name>
    """
    try:
        with open(REPORT_PATH, "a", encoding="utf-8") as f:
            f.write(f"{plate}\t{interest_name}\n")
    except Exception:
        # отчёт побочный, не должны заваливать основную логику
        pass


IGNORE_PLATES = {
    # игнор-лист от тебя
    "104039",
    "2024050601",
    "018270283681",
    "018270348452",
    # уже проверенный
    "K630AX702",
}


def _list_plates_from_cloud() -> List[str]:
    """
    Возвращает список папок верхнего уровня в облаке: plates (гос.номера).
    """
    client = cloud_uploader.client
    base = settings.CLOUD_PATH
    # до 3 попыток (WebDAV иногда таймаутит)
    for attempt in range(1, 4):
        try:
            items = client.list(base) or []
            break
        except RemoteResourceNotFound:
            return []
        except Exception as e:
            if attempt >= 3:
                print(f"[ERROR] Не удалось получить список plates в облаке ({base}): {e}")
                return []
            print(f"[WARN] Ошибка при list({base}), попытка {attempt}/3: {e}")
            time.sleep(2)

    plates: List[str] = []
    for it in items:
        name = it.rstrip("/").split("/")[-1]
        # пропускаем служебные/мусорные элементы
        if not name or name in (".", ".."):
            continue
        # по идее plate не выглядит как YYYY.MM.DD
        if DAY_RE.match(name):
            continue
        plates.append(name)
    plates = sorted(set(plates))
    return plates


def _list_days_for_plate(plate: str) -> List[str]:
    """
    Список дней (YYYY.MM.DD) для заданного номера в облаке.
    """
    client = cloud_uploader.client
    base = f"{settings.CLOUD_PATH}/{plate}"
    # до 3 попыток на случай временных таймаутов WebDAV
    for attempt in range(1, 4):
        try:
            items = client.list(base)
            break
        except RemoteResourceNotFound:
            return []
        except Exception as e:
            if attempt >= 3:
                print(f"  [ERROR] Не удалось получить список дней для {plate}: {e}")
                return []
            print(f"  [WARN] Ошибка при list({base}), попытка {attempt}/3: {e}")
            time.sleep(2)

    days: List[str] = []
    for item in items:
        name = item.rstrip("/").split("/")[-1]
        if DAY_RE.match(name):
            days.append(name)
    return sorted(days)


def _load_recognition_report(interest_folder: str) -> Optional[Dict]:
    """
    Пытается найти и скачать JSON-отчёт распознавания в папке интереса.
    Возвращает dict либо None, если отчёта нет или он не читается.
    """
    client = cloud_uploader.client

    # 1) Получаем список всех файлов в папке интереса (client.list синхронный)
    try:
        items = client.list(interest_folder) or []
    except Exception:
        items = []

    # Собираем все JSON-файлы
    json_files: List[str] = []
    for it in items:
        name = it.rstrip("/").split("/")[-1]
        if name.lower().endswith(".json"):
            json_files.append(name)

    if not json_files:
        return None

    # 2) Сначала пробуем известные имена, затем любые остальные .json
    ordered: List[str] = []
    lower_map = {n.lower(): n for n in json_files}
    for cand in REPORT_CANDIDATES:
        key = cand.lower()
        if key in lower_map:
            ordered.append(lower_map[key])
    for n in json_files:
        if n not in ordered:
            ordered.append(n)

    def _has_result_counts(obj) -> bool:
        if isinstance(obj, dict):
            res = obj.get("result")
            if isinstance(res, dict) and "counts" in res:
                return True
            # иногда результат может быть сразу dict с counts
            if "counts" in obj and isinstance(obj.get("counts"), dict):
                return True
        if isinstance(obj, list):
            return any(_has_result_counts(x) for x in obj)
        return False

    for fname in ordered:
        remote_path = f"{interest_folder}/{fname}"
        raw = cloud_uploader._download_bytes_safe(cloud_uploader.client, remote_path)  # type: ignore[attr-defined]
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            continue

        if _has_result_counts(data):
            print(f"        Найден отчёт {fname} для папки {interest_folder}")
            return data

    return None


def _has_loading_events(report: Dict) -> bool:
    """
    Считаем, что погрузка ЕСТЬ, если где‑то в отчёте есть непустой `counts`.

    Нас интересуют случаи, когда `"counts": {}` (или вообще нет ключа `counts`) —
    это означает, что модель ничего не распознала.
    """

    def _check_obj(obj) -> bool:
        # Если это один "большой" объект с полем result
        if isinstance(obj, dict):
            res = obj.get("result")
            if isinstance(res, dict):
                counts = res.get("counts")
                if isinstance(counts, dict) and len(counts) > 0:
                    return True
        return False

    # Отчёт может быть либо dict, либо списком объектов, как в примере
    if isinstance(report, dict):
        if _check_obj(report):
            return True
    elif isinstance(report, list):
        for item in report:
            if _check_obj(item):
                return True

    # Нигде не нашли непустой counts → погрузок НЕТ
    return False


def collect_interests_without_loading() -> Dict[str, List[str]]:
    """
    Обходит все регистраторы и дни в облаке и собирает интересы,
    по которым:
      - есть отчёт распознавания в папке интереса
      - но модель не нашла ни одной погрузки евро/бункера.

    Возвращает словарь:
        { plate: [interest_name, ...], ... }
    """
    result: Dict[str, List[str]] = defaultdict(list)

    total_plates = 0
    total_days = 0
    total_interests_checked = 0

    plates = _list_plates_from_cloud()
    # применяем ignore
    plates = [p for p in plates if p not in IGNORE_PLATES]
    print(f"Всего plates в облаке для обхода (после ignore): {len(plates)}")

    for plate_idx, plate in enumerate(plates, start=1):
        print(f"\n[{plate_idx}/{len(plates)}] Plate {plate}")
        total_plates += 1

        days = _list_days_for_plate(plate)
        if not days:
            print("  Дней в облаке не найдено, пропускаем.")
            continue

        print(f"  Найдено дней в облаке: {len(days)}")

        for day_str in days:
            total_days += 1
            print(f"  День {day_str}...")
            # Используем уже готовую функцию из cloud_uploader
            try:
                interest_names = cloud_uploader._list_cloud_interest_folders_for_day(  # type: ignore[attr-defined]
                    cloud_uploader.client, plate, day_str
                )
            except Exception:
                print(f"    [WARN] Не удалось получить интересы для дня {day_str}, пропускаем.")
                continue

            if not interest_names:
                print(f"    Интересов в этом дне нет.")
                continue

            print(f"    Интересов в этом дне: {len(interest_names)}")

            for name in interest_names:
                print(f"        Работаем с интересом {name}")
                total_interests_checked += 1
                # Строим путь до папки интереса
                try:
                    _, _, interest_folder = cloud_uploader.get_interest_folder_path(
                        name, dest_directory=settings.CLOUD_PATH
                    )
                except Exception as e:
                    print(f"        [ПРОПУСК] Не удалось получить путь папки: {e}")
                    continue

                # Фильтр по дате интереса: берём только интересы ПОЗЖЕ 10.11.2025
                try:
                    _, date_str, _, _ = main_funcs.parse_interest_name(name)
                    interest_date = datetime.datetime.strptime(date_str, "%Y.%m.%d").date()
                    if interest_date <= datetime.date(2025, 11, 10):
                        print(f"        [ПРОПУСК] Дата интереса {date_str} <= 10.11.2025")
                        continue
                except Exception as e:
                    print(f"        [ПРОПУСК] Не удалось распарсить дату в имени: {e}")
                    continue

                report = _load_recognition_report(interest_folder)
                # «Был прогнан через модель» — есть отчёт
                if report is None:
                    print(f"        [ПРОПУСК] Отчёт распознавания не найден в папке")
                    continue

                has_events = _has_loading_events(report)
                if has_events:
                    print(f"        [ПРОПУСК] Модель обнаружила погрузки (counts не пустой)")
                    continue

                # Модель прогнала, но погрузок нет — записываем
                result[plate].append(name)
                _append_report_line(plate, name)

    print(
        f"\nПросмотрено plates: {total_plates}, дней: {total_days}, интересов: {total_interests_checked}"
    )

    return result


def main():
    # Перезаписываем файл отчёта в начале запуска, чтобы каждый запуск формировал свой список.
    try:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write("plate\tinterest_name\n")
    except Exception:
        # если не удалось создать отчёт — продолжаем, просто не будет файла
        pass

    data = collect_interests_without_loading()

    total = sum(len(v) for v in data.values())
    print(f"Всего интересов с распознаванием, но без погрузок евро/бункера: {total}")
    print()

    for plate, interests in sorted(data.items()):
        if not interests:
            continue
        print(f"Plate {plate}: {len(interests)} интерес(ов)")
        for name in sorted(interests):
            print(f"  - {name}")
        print()


if __name__ == "__main__":
    main()

