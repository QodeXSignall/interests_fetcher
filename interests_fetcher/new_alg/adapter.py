"""
Адаптер между новым `find_interests` и форматом, который ждёт `main_operator.py`.

Существующий пайплайн (`cms_api_funcs.find_interests_by_lifting_switches`)
возвращает список dict'ов с фиксированной структурой — её ждут `pending_interests`,
`merge_overlapping_interests`, `download_clips_for_interest`. Чтобы новый
алгоритм был drop-in заменой, прогоняем `Interest` через этот адаптер.

Так же реализуем семантику `LoadingInProgress`: если ВСЕ найденные кластеры
помечены как provisional (stop_end не подтверждён, машина возможно ещё грузит)
и при этом данных свежее некоторого порога нет — пробрасываем исключение,
чтобы вышестоящий цикл подтянул новые треки.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterable

from interests_fetcher.data import settings
from interests_fetcher.logger import logger
from interests_fetcher.new_alg.algo import (
    Interest, Params, TIME_FMT, find_interests as _algo_find,
)


_TIME_FMT = TIME_FMT  # "%Y-%m-%d %H:%M:%S"


# Маппинг внутреннего cargo_type ↔ строки, которую ждёт текущий пайплайн.
# В существующем коде:
#   cargo_type = "Бункер" if kgo_on else "Контейнер"
# То есть Euro/Концевик → "Контейнер", КГО → "Бункер".
_CARGO_NEW_TO_LEGACY = {
    "euro": "Контейнер",
    "kgo": "Бункер",
}


def _ignore_points_cached() -> list[dict[str, Any]]:
    """Загружает interests_fetcher/data/ignore_points.json. Кэширует чтение."""
    try:
        path = Path(settings.IGNORE_POINTS_JSON)
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("ignore_points") or []
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"[new_alg.adapter] не смог прочитать ignore_points: {e!r}")
        return []


def _seconds_since_midnight(d: dt.datetime) -> int:
    return d.hour * 3600 + d.minute * 60 + d.second


def _interest_to_legacy_dict(it: Interest) -> dict[str, Any]:
    """Сериализует Interest в формат, совместимый со старым find_interests_by_lifting_switches."""
    st = it.interest_start_dt
    en = it.interest_end_dt
    pb = it.photo_before_dt
    pa = it.photo_after_dt
    plate = it.plate or it.reg_id

    # Имя — тот же шаблон, что и в существующем cms_interface.functions.get_interest_from_track
    name = (
        f"{plate}_"
        f"{st.year}.{st.month:02d}.{st.day:02d} "
        f"{st.hour:02d}.{st.minute:02d}.{st.second:02d}-"
        f"{en.hour:02d}.{en.minute:02d}.{en.second:02d}"
    )

    cargo_legacy = _CARGO_NEW_TO_LEGACY.get(it.cargo_type, "Контейнер")

    switch_events = []
    for f in it.cluster.fires:
        switch_events.append({
            "datetime": f.start_dt.strftime(_TIME_FMT),
            "switch": f.io,  # номер IO (1..4), как в существующем коде
            "source": f.source,  # 'alarm' | 'track'
        })

    return {
        "name": name,
        "reg_id": it.reg_id,
        "beg_sec": _seconds_since_midnight(st),
        "end_sec": _seconds_since_midnight(en),
        "year": st.year,
        "month": st.month,
        "day": st.day,
        "start_time": st.strftime(_TIME_FMT),
        "end_time": en.strftime(_TIME_FMT),
        "car_number": plate,
        "photo_before_timestamp": pb.strftime(_TIME_FMT),
        "photo_after_timestamp": pa.strftime(_TIME_FMT),
        "photo_before_sec": _seconds_since_midnight(pb),
        "photo_after_sec": _seconds_since_midnight(pa),
        "report": {
            "cargo_type": cargo_legacy,
            "geo": it.geo,
            "switches_amount": len(switch_events),
            "switch_events": switch_events,
        },
        # доп.поля для отладки/мониторинга, существующий код их игнорирует
        "_new_alg": {
            "is_provisional": it.is_provisional,
            "first_fire": it.first_fire_dt.strftime(_TIME_FMT),
            "last_fire": it.last_fire_dt.strftime(_TIME_FMT),
            "fires_count": it.fires_count,
        },
    }


def find_interests_legacy_compat(
    *,
    tracks: list[dict],
    raw_alarms: list[dict],
    reg_info: dict[str, Any],
    reg_id: str,
    last_track_dt: dt.datetime | None = None,
    in_progress_grace_min: int = 30,
    params: Params | None = None,
) -> list[dict[str, Any]]:
    """Запускает новый алгоритм и возвращает список интересов в legacy-формате.

    `last_track_dt` — gt самого свежего трека в окне; если он недавний
    (свежее `in_progress_grace_min` минут от сейчас), provisional-интересы
    отдавать ещё рано — выбрасываем `LoadingInProgress`, чтобы caller
    подождал новых треков. На исторической обработке (дамп/catch-up)
    `last_track_dt` далеко в прошлом, поэтому provisional отдаются вместе
    с finalized.
    """
    from interests_fetcher.cms_interface.functions import LoadingInProgress

    plate = (reg_info or {}).get("plate") or reg_id
    ignore_points = _ignore_points_cached()

    interests = _algo_find(
        tracks=tracks,
        alarms=raw_alarms,
        truck_info=reg_info or {},
        plate=plate,
        reg_id=reg_id,
        params=params,
        ignore_points=ignore_points,
    )

    if not interests:
        return []

    finalized = [it for it in interests if not it.is_provisional]
    pending = [it for it in interests if it.is_provisional]

    # Решаем, отдавать ли provisional. Если последний трек в окне свежий
    # (моложе grace-периода) — машина возможно ещё грузит, ждём.
    now = dt.datetime.now()
    is_live_window = (
        last_track_dt is not None
        and (now - last_track_dt).total_seconds() < in_progress_grace_min * 60
    )

    if pending and is_live_window and not finalized:
        logger.info(
            f"{reg_id}: [new_alg] есть {len(pending)} provisional-интересов и нет "
            f"finalized. Последний трек {last_track_dt} свежий — ждём подтверждения."
        )
        raise LoadingInProgress

    if pending and not is_live_window:
        # Историческое окно — provisional можно отдать с fallback-границами.
        finalized = finalized + pending

    return [_interest_to_legacy_dict(it) for it in finalized]


# -- Shadow-mode сравнение ------------------------------------------------

def _key(d: dict[str, Any]) -> tuple[str, str]:
    return (d.get("start_time", ""), d.get("end_time", ""))


def diff_for_shadow(legacy: list[dict], new: list[dict]) -> dict[str, Any]:
    """Сравнивает выходы старого и нового алгоритма для одного окна.

    Возвращает компактную сводку, которую можно залить в журнал/телеметрию.
    """
    legacy_keys = {_key(d) for d in legacy}
    new_keys = {_key(d) for d in new}

    common = legacy_keys & new_keys
    only_legacy = legacy_keys - new_keys
    only_new = new_keys - legacy_keys

    return {
        "legacy_count": len(legacy),
        "new_count": len(new),
        "common": len(common),
        "only_legacy": sorted(only_legacy),
        "only_new": sorted(only_new),
    }
