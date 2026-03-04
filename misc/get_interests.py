from  qt_pvp.interest_merge_funcs import merge_overlapping_interests
from qt_pvp import functions as main_funcs
from main_operator import Main
import asyncio


#K630AX702_2025.10.30 10.05.08-10.16.49
#K630AX702_2025.12.01 08.51.15-08.53.20
#10:05:07
# 10:16:42
#REG_ID = "108411"
REG_ID = "108410"
START_TIME = "2026-02-19 06:00:00"
END_TIME = "2026-02-20 18:00:00"


inst = Main()
reg_info = main_funcs.get_reg_info(reg_id=REG_ID)


async def local_get_interests_async():
    await inst.login()

    interests = await inst.get_interests_async(
        reg_id=REG_ID,
        reg_info=reg_info,
        start_time=START_TIME,
        stop_time=END_TIME,
    )
    interests = merge_overlapping_interests(interests)
    print(f"\n=== РЕЗУЛЬТАТ ===")
    print(f"Найдено интересов: {len(interests)}")
    for interest in interests:
        print(interest)


if __name__ == "__main__":
    asyncio.run(local_get_interests_async())

real_loads = [
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 07:27:10', 'time_end': '2025-11-13 08:28:30', 'trouble': 'Зафиксировано поздно начало'},
{'type': 'euro', 'amount': 4, 'time_start': '2025-11-13 07:35:18', 'time_end': '2025-11-13 07:38:22', 'trouble': 'Не зафиксировано!'},
{'type': 'euro', 'amount': 3, 'time_start': '2025-11-13 07:43:36', 'time_end': '2025-11-13 07:46:15', 'trouble': None},
{'type': 'bunker', 'amount': 1, 'time_start': '2025-11-13 08:00:49', 'time_end': '2025-11-13 08:05:09', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 08:08:35', 'time_end': '2025-11-13 08:09:45', 'trouble': None},
{'type': 'euro', 'amount': 3, 'time_start': '2025-11-13 08:12:00', 'time_end': '2025-11-13 08:13:50', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 08:16:20', 'time_end': '2025-11-13 08:17:00', 'trouble': 'Не зафиксировано'},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 08:21:00', 'time_end': '2025-11-13 08:21:50', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 08:24:40', 'time_end': '2025-11-13 08:25:40', 'trouble': 'Не зафиксировано'},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 08:35:30', 'time_end': '2025-11-13 08:37:20', 'trouble': 'Не зафиксировано'},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 09:11:27', 'time_end': '2025-11-13 09:12:30', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 09:20:45', 'time_end': '2025-11-13 09:21:45', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 09:23:45', 'time_end': '2025-11-13 09:24:40', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 09:36:18', 'time_end': '2025-11-13 09:37:15', 'trouble': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 09:39:15', 'time_end': '2025-11-13 09:40:30', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 09:53:03', 'time_end': '2025-11-13 09:54:10', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 10:00:35 ', 'time_end': '2025-11-13 10:02:15', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 10:05:10 ', 'time_end': '2025-11-13 10:06:35', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 10:08:20  ', 'time_end': '2025-11-13 10:09:20', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 10:12:45  ', 'time_end': '2025-11-13 10:13:30', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 10:36:40', 'time_end': '2025-11-13 10:39:00', 'trouble': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 10:45:20', 'time_end': '2025-11-13 10:47:00', 'note': 'Забросили пакет и мусор из контейнера'},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 10:50:00', 'time_end': '2025-11-13 10:51:40', 'note': 'Контейнер не опустился после разгрузки как надо'},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 11:06:27', 'time_end': '2025-11-13 11:08:00', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 11:10:25', 'time_end': '2025-11-13 11:40:00', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 11:14:10', 'time_end': '2025-11-13 11:15:15 ', 'note': None},
{'type': 'bunker', 'amount': 1, 'time_start': '2025-11-13 11:16:00', 'time_end': '2025-11-13 11:20:37 ', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 11:25:47', 'time_end': '2025-11-13 11:27:30 ', 'note': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 11:35:00', 'time_end': '2025-11-13 11:36:35', 'note': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 11:38:13', 'time_end': '2025-11-13 11:39:20', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 11:40:55', 'time_end': '2025-11-13 11:42:40', 'note': None},
{'type': 'bunker', 'amount': 1, 'time_start': '2025-11-13 11:48:00', 'time_end': '2025-11-13 11:55:00', 'note': None},
{'type': 'euro', 'amount': 4, 'time_start': '2025-11-13 11:59:00', 'time_end': '2025-11-13 12:02:00', 'note': None},
{'type': 'bunker', 'amount': 1, 'time_start': '2025-11-13 12:04:30', 'time_end': '2025-11-13 12:16:30', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 12:21:00', 'time_end': '2025-11-13 12:22:40', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 12:23:44', 'time_end': '2025-11-13 12:25:40', 'note': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 12:28:20 ', 'time_end': '2025-11-13 12:29:30', 'note': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 12:30:45 ', 'time_end': '2025-11-13 12:31:50', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 12:34:50 ', 'time_end': '2025-11-13 12:36:10', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 12:47:27 ', 'time_end': '2025-11-13 12:48:10', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 12:50:40 ', 'time_end': '2025-11-13 12:52:15', 'note': None},
{'type': 'bunker', 'amount': 1, 'time_start': '2025-11-13 12:52:50 ', 'time_end': '2025-11-13 12:57:04', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 12:59:30 ', 'time_end': '2025-11-13 13:01:10', 'note': None},
{'type': 'euro', 'amount': 3, 'time_start': '2025-11-13 13:04:10 ', 'time_end': '2025-11-13 13:06:30', 'note': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 13:07:56 ', 'time_end': '2025-11-13 13:09:20', 'note': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 13:11:10 ', 'time_end': '2025-11-13 13:12:15', 'note': None},
{'type': 'euro', 'amount': 1, 'time_start': '2025-11-13 13:15:10 ', 'time_end': '2025-11-13 13:16:10', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 13:17:10 ', 'time_end': '2025-11-13 13:18:30', 'note': None},
{'type': 'euro', 'amount': 2, 'time_start': '2025-11-13 13:19:48 ', 'time_end': '2025-11-13 13:21:20', 'note': None},
{'type': 'euro', 'amount': 3, 'time_start': '2025-11-13 13:24:30 ', 'time_end': '2025-11-13 13:26:50', 'note': None},
{'type': 'bunker', 'amount': 1, 'time_start': '2025-11-13 13:28:20 ', 'time_end': '2025-11-13 13:35:00', 'note': None},

]
points = [
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 07:27:10", "time_end": "2025-11-13 08:28:30", "trouble": "Зафиксировано поздно начало"},
    {"type": "euro", "amount": 4, "time_start": "2025-11-13 07:35:18", "time_end": "2025-11-13 07:38:22", "trouble": "Не зафиксировано!"},
    {"type": "euro", "amount": 3, "time_start": "2025-11-13 07:43:36", "time_end": "2025-11-13 07:46:15", "trouble": None},

    {"type": "bunker", "amount": 1, "time_start": "2025-11-13 08:00:49", "time_end": "2025-11-13 08:05:09",  "trouble": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 08:08:35", "time_end": "2025-11-13 08:09:45", "trouble": None},
    {"type": "euro", "amount": 3, "time_start": "2025-11-13 08:12:00", "time_end": "2025-11-13 08:13:50", "trouble": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 08:16:20", "time_end": "2025-11-13 08:17:00",
     "trouble": "Не зафиксировано"},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 08:21:00", "time_end": "2025-11-13 08:21:50",
     "trouble": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 08:24:40", "time_end": "2025-11-13 08:25:40",
     "trouble": "Не зафиксировано"},
    {"type": "bag", "amount": 0, "time_start": "2025-11-13 08:31:00", "time_end": "2025-11-13 08:31:40",
     "trouble": None, "note": "Пакет из контейнера"},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 08:35:30", "time_end": "2025-11-13 08:37:20",
     "trouble": "Не зафиксировано"},
    {"type": "bag", "amount": 0, "time_start": "2025-11-13 08:40:50", "time_end": "2025-11-13 08:41:50",
     "trouble": None, "note": "Пакет из контейнера"},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 08:45:52", "time_end": "2025-11-13 08:47:24",
     "trouble": None, "note": "Ничего не забрали, пустые контейнера"},
    {"type": "bag", "amount": 0, "time_start": "2025-11-13 08:54:54", "time_end": "2025-11-13 08:55:55",
     "trouble": None, "note": "Побросали мусор с площадки"},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 08:57:50", "time_end": "2025-11-13 08:58:30",
     "trouble": None, "note": "Остановились, посмотрели из машины на пустые контейнеры и поехали дальше"},

    {"type": "bag", "amount": 0, "time_start": "2025-11-13 09:01:45", "time_end": "2025-11-13 09:02:58",
     "trouble": None, "note": "Побросали пакеты, уехали, хотя контейнеры были полные"},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 09:06:00", "time_end": "2025-11-13 09:06:40",
     "trouble": None, "note": "Остановились, посмотрели из машины на пустые контейнеры и поехали дальше"},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 09:11:27", "time_end": "2025-11-13 09:12:30",
     "trouble": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 09:20:45", "time_end": "2025-11-13 09:21:45",
     "trouble": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 09:23:45", "time_end": "2025-11-13 09:24:40",
     "trouble": None},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 09:30:31", "time_end": "2025-11-13 09:30:36",
     "trouble": "Остановились, посмотрели из машины на пустые контейнеры и поехали дальше"},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 09:36:18", "time_end": "2025-11-13 09:37:15",
     "trouble": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 09:39:15", "time_end": "2025-11-13 09:40:30",
     "trouble": None},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 09:43:53", "time_end": "2025-11-13 09:44:55",
     "trouble": None, "note": "Увидели пустые контейнера, походили, сфоткали и уехали"},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 09:47:50", "time_end": "2025-11-13 09:48:20",
     "trouble": None, "note": "Увидели пустые контейнера, походили, сфоткали и уехали"},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 09:53:03", "time_end": "2025-11-13 09:54:10",
     "trouble": None},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 09:56:04", "time_end": "2025-11-13 09:56:50",
     "trouble": None, "note": "Контейнера пустые, выровняли их"},

    {"type": "euro", "amount": 1, "time_start": "2025-11-13 10:00:35 ", "time_end": "2025-11-13 10:02:15",
     "trouble": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 10:05:10 ", "time_end": "2025-11-13 10:06:35",
     "trouble": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 10:08:20  ", "time_end": "2025-11-13 10:09:20",
     "trouble": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 10:12:45  ", "time_end": "2025-11-13 10:13:30",
     "trouble": None},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 10:22:00  ", "time_end": "2025-11-13 10:23:00",
     "trouble": None, "note": "Пустые контейнера, походили рядом, сфоткали и уехали"},
    {"type": "bag", "amount": 0, "time_start": "2025-11-13 10:28:28", "time_end": "2025-11-13 10:29:30",
     "trouble": None, "note": "Забросили мешок из контейнера и уехали"},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 10:32:00", "time_end": "2025-11-13 10:33:00",
     "trouble": None, "note": "Пустые контейнера, походили рядом, сфоткали и уехали"},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 10:36:40", "time_end": "2025-11-13 10:39:00",
     "trouble": None},
    {"type": "bag", "amount": 0, "time_start": "2025-11-13 10:40:16", "time_end": "2025-11-13 10:41:40",
     "note": "Забросили пакет и мусор из контейнера"},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 10:45:20", "time_end": "2025-11-13 10:47:00",
     "note": "Забросили пакет и мусор из контейнера"},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 10:50:00", "time_end": "2025-11-13 10:51:40",
     "note": "Контейнер не опустился после разгрузки как надо"},

    {"type": "euro", "amount": 2, "time_start": "2025-11-13 11:06:27", "time_end": "2025-11-13 11:08:00",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 11:10:25", "time_end": "2025-11-13 11:40:00",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 11:14:10", "time_end": "2025-11-13 11:15:15 ",
     "note": None},
    {"type": "bunker", "amount": 1, "time_start": "2025-11-13 11:16:00", "time_end": "2025-11-13 11:20:37 ",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 11:25:47", "time_end": "2025-11-13 11:27:30 ",
     "note": None},
    {"type": "bag", "amount": 0, "time_start": "2025-11-13 11:30:10 ", "time_end": "2025-11-13 11:30:54",
     "note": "Забросили мешок из контейнера"},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 11:35:00", "time_end": "2025-11-13 11:36:35",
     "note": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 11:38:13", "time_end": "2025-11-13 11:39:20",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 11:40:55", "time_end": "2025-11-13 11:42:40",
     "note": None},
    {"type": "bunker", "amount": 1, "time_start": "2025-11-13 11:48:00", "time_end": "2025-11-13 11:55:00",
     "note": None},
    {"type": "euro", "amount": 4, "time_start": "2025-11-13 11:59:00", "time_end": "2025-11-13 12:02:00",
     "note": None},
    {"type": "bunker", "amount": 1, "time_start": "2025-11-13 12:04:30", "time_end": "2025-11-13 12:16:30",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 12:21:00", "time_end": "2025-11-13 12:22:40",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 12:23:44", "time_end": "2025-11-13 12:25:40",
     "note": None},
    {"type": "euro", "amount": 1 , "time_start": "2025-11-13 12:28:20 ", "time_end": "2025-11-13 12:29:30",
     "note": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 12:30:45 ", "time_end": "2025-11-13 12:31:50",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 12:34:50 ", "time_end": "2025-11-13 12:36:10",
     "note": None},
    {"type": "empty", "amount": 0, "time_start": "2025-11-13 12:39:37 ", "time_end": "2025-11-13 12:40:30",
     "note": "Выровняли пустые контейнера и поехали",},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 12:47:27 ", "time_end": "2025-11-13 12:48:10",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 12:50:40 ", "time_end": "2025-11-13 12:52:15",
     "note": None},
    {"type": "bunker", "amount": 1, "time_start": "2025-11-13 12:52:50 ", "time_end": "2025-11-13 12:57:04",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 12:59:30 ", "time_end": "2025-11-13 13:01:10",
     "note": None},

    {"type": "euro", "amount": 3, "time_start": "2025-11-13 13:04:10 ", "time_end": "2025-11-13 13:06:30",
     "note": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 13:07:56 ", "time_end": "2025-11-13 13:09:20",
     "note": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 13:11:10 ", "time_end": "2025-11-13 13:12:15",
     "note": None},
    {"type": "euro", "amount": 1, "time_start": "2025-11-13 13:15:10 ", "time_end": "2025-11-13 13:16:10",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 13:17:10 ", "time_end": "2025-11-13 13:18:30",
     "note": None},
    {"type": "euro", "amount": 2, "time_start": "2025-11-13 13:19:48 ", "time_end": "2025-11-13 13:21:20",
     "note": None},
    {"type": "euro", "amount": 3, "time_start": "2025-11-13 13:24:30 ", "time_end": "2025-11-13 13:26:50",
     "note": None},
    {"type": "bunker", "amount": 1, "time_start": "2025-11-13 13:28:20 ", "time_end": "2025-11-13 13:35:00",
     "note": None},
]

#for point in points:
#    if point["type"] in ("euro", "bunker") or point["amount"] > 0:
#        print(point)