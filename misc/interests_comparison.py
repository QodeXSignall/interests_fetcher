from webdav3.client import Client
from webdav3.exceptions import RemoteResourceNotFound
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Set
from interests_fetcher import functions as main_funcs
from interests_fetcher.interest_merge_funcs import merge_overlapping_interests
from main_operator import Main
import asyncio
import os


WEBDAV_OPTIONS = {
    "webdav_hostname": os.environ["webdav_hostname"],
    "webdav_login":    os.environ["webdav_login"],
    "webdav_password": os.environ["webdav_password"],
    # если нужен, добавь "webdav_root": "/"
}

BASE_PATH   = "/Tracker/Видео выгрузок"
REG_ID      = "108411"               # номер регистратора (папка верхнего уровня)
DAY_STR     = "2025.09.12"           # папка дня
TIME_FMT_FN = "%Y.%m.%d %H.%M.%S"    # в названиях папок
TIME_FMT    = "%Y-%m-%d %H:%M:%S"    # в get_interests

REGS_PLATES = {
    "108411": "A939CA702",
    "108410": "К180КЕ702",
    "018270348452": "K630AX702"
}


def list_interest_folders(client: Client, base_path: str, reg_id: str, day_str: str) -> List[str]:
    day_path = f"{base_path}/{reg_id}/{day_str}"
    items = client.list(day_path)
    # WebDAV client возвращает и саму папку; фильтруем только «папки интересов»
    names = []
    for item in items:
        # обычно item = '.../A939CA702_2025.08.17 10.35.39-10.37.47/'
        name = item.rstrip("/").split("/")[-1]
        if "_" in name and "-" in name and "." in name:
            names.append(name)
    return sorted(names)

def parse_folder_name(name: str) -> Tuple[str, datetime, datetime]:
    # A939CA702_2025.08.17 10.35.39-10.37.47
    plate, rest = name.split("_", 1)
    left, right = rest.split("-")
    start_dt = datetime.strptime(left.strip(), TIME_FMT_FN)
    # у right нет даты — берём дату из левой части
    date_prefix = start_dt.strftime("%Y.%m.%d")
    end_dt = datetime.strptime(f"{date_prefix} {right.strip()}", TIME_FMT_FN)
    return plate, start_dt, end_dt

def fuzzy_equal(n1: str, n2: str, eps_sec: int = 10) -> bool:
    """Фаззи-сравнение имён: равны plate и даты, старты/концы ±eps."""
    try:
        p1, s1, e1 = parse_folder_name(n1)
        p2, s2, e2 = parse_folder_name(n2)
    except Exception:
        return False
    return (
        p1 == p2 and
        abs((s1 - s2).total_seconds()) <= eps_sec and
        abs((e1 - e2).total_seconds()) <= eps_sec
    )

def diff_sets(expected: Set[str], detected: Set[str], eps_sec: int = 0):
    new = set(detected)
    missing = set(expected)
    if eps_sec <= 0:
        return new - expected, missing - detected

    matched_exp = set()
    matched_det = set()

    for e in expected:
        for d in detected:
            if d in matched_det:
                continue

            # 1) сначала точное совпадение строкой — вообще без дат
            if e == d:
                matched_exp.add(e)
                matched_det.add(d)
                break

            # 2) затем уже фаззи через парсинг
            if fuzzy_equal(e, d, eps_sec=eps_sec):
                matched_exp.add(e)
                matched_det.add(d)
                break

    new -= matched_det
    missing -= matched_exp
    return new, missing


async def main(day_str = DAY_STR, reg_id = REG_ID):
    print(f"\nWorking with day {day_str}")
    client = Client(WEBDAV_OPTIONS)

    # 1) Эталон из WebDAV
    plate_num = REGS_PLATES[reg_id]
    try:
        folder_names = list_interest_folders(client, BASE_PATH, plate_num, day_str)
    except RemoteResourceNotFound:
        print(f"День {day_str} не найден в cloud.")
        return
    if not folder_names:
        print("[WARN] В этот день не найдено эталонных интересов на WebDAV.")
        return

    # 2) Интервал для анализа = весь день от 00:00:00 до 23:59:59
    _, s_first, _ = parse_folder_name(folder_names[0])
    # Используем дату из первого интереса, но устанавливаем время на начало дня
    start_time = s_first.date().strftime("%Y-%m-%d") + " 00:00:00"
    # Конец дня - 23:59:59
    stop_time = s_first.date().strftime("%Y-%m-%d") + " 23:59:59"

    # 3) Поиск интересов в системе
    inst = Main()
    await inst.login()
    reg_info = main_funcs.get_reg_info(reg_id=reg_id)
    interests = await inst.get_interests_async(reg_id=reg_id, reg_info=reg_info,
                                   start_time=start_time, stop_time=stop_time)
    interests = merge_overlapping_interests(interests)
    print(f"\Total cloud interests: {len(folder_names)}")
    for interest in folder_names:
        print(f"\t{interest}")
    print(f"\nTotal found interests: {len(interests)}")
    for interest in interests:
        print(f"\t{interest['name']}")
    detected_names = set(i["name"] for i in interests)

    expected_names = set(folder_names)

    # 4) Сравнение: сначала строгая, затем фаззи (±10с)
    #new_strict, missing_strict = diff_sets(expected_names, detected_names, eps_sec=0)
    new_fuzzy,  missing_fuzzy  = diff_sets(expected_names, detected_names, eps_sec=30)

    #print("=== STRICT ===")
    #print("Новые интересы (не были в WebDAV):")
    #for n in sorted(new_strict): print("  +", n)
    #print("Не найденные новым алгоритмом интересы (В webdav они есть)")
    #for n in sorted(missing_strict): print("  -", n)
    print("=== Fazzy ===")
    print("Новые интересы (не были в WebDAV):")
    for n in sorted(new_fuzzy): print("  +", n)
    print("Не найденные новым алгоритмом интересы (В webdav они есть)")
    for n in sorted(missing_fuzzy): print("  -", n)


if __name__ == "__main__":
    day = "2025.12.22"
    reg_id = "108411"
    #reg_id = "018270348452"
    asyncio.run(main(day_str=day, reg_id=reg_id))
