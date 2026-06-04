"""
Прогон алгоритма из algo.py по дампу new_alg/raw_data/* и сравнение с ground_truth.json.

Выводы для каждого (plate, date):
  - сколько интересов нашёл алгоритм vs сколько у контрактора;
  - матч по перекрытию времени (контрактор-событие ↔ интерес);
  - средние/p95 отклонения по interest_start / interest_end;
  - пропуски (FN) и ложные срабатывания (FP) с их временами.

Запуск:  python3 -m interests_fetcher.new_alg.validate
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any

# Делаем модуль запускаемым как `python3 -m interests_fetcher.new_alg.validate`
# а также как голый `python3 .../validate.py` — для второго добавим путь.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from interests_fetcher.new_alg.algo import (  # noqa: E402
    TIME_FMT, Params, find_interests,
)


RAW_DIR = Path(__file__).resolve().parent / "raw_data"
GT_PATH = Path(__file__).resolve().parent / "ground_truth.json"
IGNORE_POINTS_PATH = _ROOT / "interests_fetcher" / "data" / "ignore_points.json"
_IGNORE_POINTS = json.loads(IGNORE_POINTS_PATH.read_text(encoding="utf-8")).get("ignore_points", [])

# Какие типы из отчёта контрактора считаем «погрузками».
# «Колесо» — отдельный edge-case (после видеоразбора 26.05): были случаи когда
# исполнитель писал «Заброс», а на видео оказалось что водитель использовал
# подъёмник для погрузки колеса. Датчик при этом срабатывает корректно — для
# алгоритма это валидная погрузка.
LOADING_TYPES = {"Евро", "Бункер", "Колесо"}
# Что игнорируем: «Заброс» (сброс без подъёма), «Пусто»/«Пустой» (пустой контейнер).
# Эти события датчик не активируют, алгоритм их находить не должен.

# Маппинг plate → reg_id + truck_info. Берём из states.json приложения,
# где это всё уже есть. Если в states нет — fallback на ручной маппинг
# (для Е028УУ702, которой нет в states.json).
_STATES = json.loads((_ROOT / "interests_fetcher" / "data" / "states.json").read_text())
_FALLBACK_TRUCK_INFO = {
    "Е028УУ702": {
        "id": "111352",
        "plate": "Е028УУ702",
        "model": "?",
        # Без подтверждения от states.json — оставим разумный дефолт по K745OH702/К483РХ702:
        "euro_container_alarm": 2, "kgo_container_alarm": 1,
        "euro_container_alarm_type": "?", "kgo_container_alarm_type": "?",
    },
}


# Транслитерация «латиница ↔ кириллица» для одинаково выглядящих символов в госномерах.
_PLATE_TRANSLIT = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
})


def _plate_key(s: str) -> str:
    return (s or "").upper().translate(_PLATE_TRANSLIT)


def find_truck_info(plate: str) -> dict[str, Any]:
    needle = _plate_key(plate)
    for _uid, info in _STATES.get("trucks", {}).items():
        ti = info.get("truck_info") or {}
        if _plate_key(ti.get("plate") or "") == needle:
            return ti
    for fb_plate, ti in _FALLBACK_TRUCK_INFO.items():
        if _plate_key(fb_plate) == needle:
            return ti
    return {}


def _dump_dir_for(plate: str) -> Path | None:
    needle = _plate_key(plate)
    for p in RAW_DIR.iterdir():
        if p.is_dir() and _plate_key(p.name) == needle:
            return p
    return None


def _gt_to_dt(date: str, hms: str) -> dt.datetime | None:
    """Толерантный парсер 'H:MM:SS' / 'HH:MM:SS' / 'HH:MM.SS' (опечатки в отчёте).

    Возвращает None если строку не получилось разобрать.
    """
    raw = (hms or "").strip().replace(".", ":")
    parts = raw.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(p) for p in parts)
    except ValueError:
        return None
    try:
        d = dt.datetime.strptime(date, "%Y-%m-%d").date()
        return dt.datetime(d.year, d.month, d.day, h, m, s)
    except ValueError:
        return None


def _overlap_seconds(a_start: dt.datetime, a_end: dt.datetime,
                     b_start: dt.datetime, b_end: dt.datetime) -> float:
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    return max(0.0, (e - s).total_seconds())


def match_interests_to_gt(
    interests: list[dict[str, Any]],
    gt_events: list[dict[str, Any]],
    date: str,
) -> dict[str, Any]:
    """Жадный матч 1-к-1 по перекрытию времени.

    Один контрактный event Евро/Бункер → один интерес алгоритма (или ничей).
    Возвращает структуру с матчами и отклонениями.
    """

    # Парсим события контрактора
    gt_parsed = []
    parse_errors = []
    for ev in gt_events:
        if ev["type"] not in LOADING_TYPES:
            continue
        s = _gt_to_dt(date, ev["start"])
        e = _gt_to_dt(date, ev["end"])
        if s is None or e is None:
            parse_errors.append(ev)
            continue
        if e < s:
            # бывают опечатки end < start; пропустим
            parse_errors.append(ev)
            continue
        gt_parsed.append({"start": s, "end": e, "type": ev["type"], "count": ev.get("count"),
                          "raw": ev})

    # Готовим алгоритмические интересы (только Евро/Бункер — мы пока ничего другого и не ищем).
    alg_parsed = []
    for it in interests:
        s = dt.datetime.strptime(it["interest_start"], TIME_FMT)
        e = dt.datetime.strptime(it["interest_end"], TIME_FMT)
        pb = dt.datetime.strptime(it["photo_before_timestamp"], TIME_FMT)
        pa = dt.datetime.strptime(it["photo_after_timestamp"], TIME_FMT)
        alg_parsed.append({"start": s, "end": e, "pb": pb, "pa": pa, "interest": it})

    # Построим матрицу пересечений (gt × alg).
    matches: list[dict[str, Any]] = []
    used_alg = set()
    used_gt = set()

    # Жадно: для каждого gt берём alg с максимальным перекрытием (если > 0).
    pairs = []
    for gi, g in enumerate(gt_parsed):
        for ai, a in enumerate(alg_parsed):
            ov = _overlap_seconds(g["start"], g["end"], a["start"], a["end"])
            if ov > 0:
                pairs.append((ov, gi, ai))
    pairs.sort(reverse=True)
    for ov, gi, ai in pairs:
        if gi in used_gt or ai in used_alg:
            continue
        used_gt.add(gi); used_alg.add(ai)
        g = gt_parsed[gi]; a = alg_parsed[ai]
        matches.append({
            "gt": g, "alg": a, "overlap_sec": ov,
            "start_diff_sec": (a["start"] - g["start"]).total_seconds(),
            "end_diff_sec": (a["end"] - g["end"]).total_seconds(),
            "pb_diff_sec": (a["pb"] - g["start"]).total_seconds(),
            "pa_diff_sec": (a["pa"] - g["end"]).total_seconds(),
        })

    # Близкие, но не перекрывшиеся (ближайший по центру) — для диагностики.
    near_misses = []
    for gi, g in enumerate(gt_parsed):
        if gi in used_gt:
            continue
        g_mid = g["start"] + (g["end"] - g["start"]) / 2
        best = None
        for ai, a in enumerate(alg_parsed):
            if ai in used_alg:
                continue
            a_mid = a["start"] + (a["end"] - a["start"]) / 2
            d = abs((a_mid - g_mid).total_seconds())
            if best is None or d < best["delta_sec"]:
                best = {"gi": gi, "ai": ai, "delta_sec": d}
        if best is not None:
            near_misses.append(best)

    fn = [gt_parsed[gi] for gi in range(len(gt_parsed)) if gi not in used_gt]
    fp = [alg_parsed[ai] for ai in range(len(alg_parsed)) if ai not in used_alg]
    return {
        "gt_count": len(gt_parsed),
        "alg_count": len(alg_parsed),
        "matched": matches,
        "false_negatives": fn,
        "false_positives": fp,
        "near_misses": near_misses,
        "gt_parse_errors": parse_errors,
    }


def run_one(plate: str, date: str, gt_events: list[dict[str, Any]],
            params: Params | None = None) -> dict[str, Any]:
    truck_info = find_truck_info(plate)
    if not truck_info:
        return {"plate": plate, "date": date, "error": "no truck_info"}

    dump_dir = _dump_dir_for(plate) or (RAW_DIR / plate)
    dump_path = dump_dir / f"{date}.json"
    if not dump_path.exists():
        return {"plate": plate, "date": date, "error": f"no dump {dump_path}"}
    data = json.loads(dump_path.read_text())
    tracks = data.get("tracks") or []
    alarms = data.get("alarms") or []

    interests = find_interests(
        tracks=tracks,
        alarms=alarms,
        truck_info=truck_info,
        plate=plate,
        reg_id=str(truck_info.get("id") or ""),
        params=params,
        ignore_points=_IGNORE_POINTS,
    )
    interests_dict = [i.to_dict() for i in interests]
    return {
        "plate": plate,
        "date": date,
        "truck_info": {
            "id": truck_info.get("id"),
            "euro_container_alarm": truck_info.get("euro_container_alarm"),
            "kgo_container_alarm": truck_info.get("kgo_container_alarm"),
            "sensor": truck_info.get("euro_container_alarm_type"),
        },
        "interests": interests_dict,
        "compare": match_interests_to_gt(interests_dict, gt_events, date),
    }


def summarize(report: dict[str, Any]) -> str:
    if "error" in report:
        return f"  ! {report['error']}"
    c = report["compare"]
    n_match = len(c["matched"])
    n_gt = c["gt_count"]
    n_alg = c["alg_count"]
    recall = n_match / n_gt * 100 if n_gt else 0
    precision = n_match / n_alg * 100 if n_alg else 0
    out = []
    out.append(f"  GT={n_gt}  ALG={n_alg}  matched={n_match}  "
               f"recall={recall:.1f}%  precision={precision:.1f}%")
    if c["matched"]:
        def _stats(label: str, diffs: list[float]) -> str:
            ds = sorted(abs(x) for x in diffs)
            p95 = ds[int(len(ds)*0.95)] if len(ds) >= 5 else ds[-1]
            return (f"    Δ{label}: median={statistics.median(diffs):+.1f}s  "
                    f"|mean|={statistics.fmean(abs(x) for x in diffs):.1f}s  "
                    f"p95(abs)={p95:.0f}s")
        out.append(_stats("istart", [m["start_diff_sec"] for m in c["matched"]]))
        out.append(_stats("iend  ", [m["end_diff_sec"] for m in c["matched"]]))
        out.append(_stats("pbefor", [m["pb_diff_sec"] for m in c["matched"]]))
        out.append(_stats("pafter", [m["pa_diff_sec"] for m in c["matched"]]))
    if c["false_negatives"]:
        out.append(f"    FN (контрактор нашёл, мы — нет): {len(c['false_negatives'])}")
        for fn in c["false_negatives"][:6]:
            out.append(f"      - {fn['start'].strftime('%H:%M:%S')}–{fn['end'].strftime('%H:%M:%S')}  ({fn['type']} cnt={fn['count']})")
        if len(c["false_negatives"]) > 6:
            out.append(f"      ... +{len(c['false_negatives']) - 6}")
    if c["false_positives"]:
        out.append(f"    FP (алгоритм нашёл, контрактор — нет): {len(c['false_positives'])}")
        for fp in c["false_positives"][:6]:
            it = fp["interest"]
            out.append(f"      - {fp['start'].strftime('%H:%M:%S')}–{fp['end'].strftime('%H:%M:%S')}  "
                       f"({it['cargo_type']} fires={it['fires_count']})")
        if len(c["false_positives"]) > 6:
            out.append(f"      ... +{len(c['false_positives']) - 6}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    gt = json.loads(GT_PATH.read_text())
    params = Params()
    print(f"params: {params}")
    print()

    reports = []
    totals = {"gt": 0, "alg": 0, "matched": 0, "fn": 0, "fp": 0,
              "start_diffs": [], "end_diffs": [], "pb_diffs": [], "pa_diffs": []}
    for entry in gt:
        plate = entry["plate"]
        date = entry["date"]
        # пропустим день, для которого контрактор написал «не погружала» — там нечего матчить,
        # но при этом полезно знать, что и алгоритм ничего не нашёл (FP-проверка).
        gt_events = entry.get("events") or []
        rep = run_one(plate, date, gt_events, params=params)
        reports.append(rep)
        print(f"==[ {plate} / {date} ]== {entry.get('note') or ''}")
        print(summarize(rep))
        if "compare" in rep:
            c = rep["compare"]
            totals["gt"] += c["gt_count"]
            totals["alg"] += c["alg_count"]
            totals["matched"] += len(c["matched"])
            totals["fn"] += len(c["false_negatives"])
            totals["fp"] += len(c["false_positives"])
            totals["start_diffs"] += [m["start_diff_sec"] for m in c["matched"]]
            totals["end_diffs"] += [m["end_diff_sec"] for m in c["matched"]]
            totals["pb_diffs"] += [m["pb_diff_sec"] for m in c["matched"]]
            totals["pa_diffs"] += [m["pa_diff_sec"] for m in c["matched"]]
        print()

    print("="*80)
    print(f"TOTAL: GT={totals['gt']}  ALG={totals['alg']}  matched={totals['matched']}  "
          f"recall={totals['matched']/totals['gt']*100 if totals['gt'] else 0:.1f}%  "
          f"precision={totals['matched']/totals['alg']*100 if totals['alg'] else 0:.1f}%")
    if totals["start_diffs"]:
        for name, arr in (("istart", totals["start_diffs"]),
                          ("iend  ", totals["end_diffs"]),
                          ("pbefor", totals["pb_diffs"]),
                          ("pafter", totals["pa_diffs"])):
            ds = sorted(abs(x) for x in arr)
            p95 = ds[int(len(ds)*0.95)] if len(ds) >= 5 else ds[-1]
            print(f"Δ{name}: median={statistics.median(arr):+.1f}s |mean|={statistics.fmean(abs(x) for x in arr):.1f}s  "
                  f"p95(abs)={p95:.0f}s")

    out_path = Path(__file__).resolve().parent / "validate_results.json"
    out_path.write_text(json.dumps(_make_serializable(reports), ensure_ascii=False, indent=2))
    print(f"\nДетальный отчёт: {out_path}")
    return 0


def _make_serializable(obj):
    """Превращаем datetime в строки, чтобы json.dump не падал на FN/FP-структурах."""
    if isinstance(obj, dt.datetime):
        return obj.strftime(TIME_FMT)
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_serializable(v) for v in obj]
    return obj


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
