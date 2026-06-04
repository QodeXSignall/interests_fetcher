"""
Печатает для каждого оставшегося FP/FN: что в данных, на каких алармах сработал
алгоритм (или нет), где это случилось — чтобы пользователь мог проверить по видео.

Запуск:  python3 -m interests_fetcher.new_alg.print_open_issues
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from interests_fetcher.new_alg.algo import TIME_FMT, ATP_TO_IO  # noqa: E402

RES = Path(__file__).resolve().parent / "validate_results.json"
RAW = Path(__file__).resolve().parent / "raw_data"


def _alarms_in(plate: str, date: str, t_lo: dt.datetime, t_hi: dt.datetime) -> list[dict]:
    dump_dir = None
    for p in RAW.iterdir():
        if p.is_dir() and p.name.upper() == plate.upper():
            dump_dir = p
            break
    if dump_dir is None:
        return []
    path = dump_dir / f"{date}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    out = []
    for a in data["alarms"]:
        bts = a.get("bTimeStr")
        ets = a.get("eTimeStr") or bts
        if not isinstance(bts, str) or not isinstance(ets, str):
            continue
        try:
            bt = dt.datetime.strptime(bts, TIME_FMT)
            et = dt.datetime.strptime(ets, TIME_FMT)
        except ValueError:
            continue
        # пересечение с окном
        if et < t_lo or bt > t_hi:
            continue
        out.append({
            "bt": bts, "et": ets,
            "atp": a.get("atp"),
            "io": ATP_TO_IO.get(a.get("atp")),
            "ssp": a.get("ssp"), "esp": a.get("esp"),
            "dur": (et - bt).total_seconds(),
            "lat_lng_start": a.get("sps"),
        })
    return out


def _parse_dt(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, TIME_FMT)


def main() -> int:
    reports = json.loads(RES.read_text())
    print("# Открытые вопросы для видеоразбора\n")

    for rep in reports:
        if "compare" not in rep:
            continue
        c = rep["compare"]
        plate = rep["plate"]
        date = rep["date"]
        truck = rep["truck_info"]
        if not c["false_positives"] and not c["false_negatives"]:
            continue
        print(f"\n## {plate} / {date}  "
              f"(IO_euro={truck.get('euro_container_alarm')}  "
              f"IO_kgo={truck.get('kgo_container_alarm')}  "
              f"sensor={truck.get('sensor')})")

        for fp in c["false_positives"]:
            it = fp["interest"]
            s = _parse_dt(it["interest_start"])
            e = _parse_dt(it["interest_end"])
            window_lo = s - dt.timedelta(seconds=60)
            window_hi = e + dt.timedelta(seconds=60)
            alarms = _alarms_in(plate, date, window_lo, window_hi)
            print(f"\n[FP] photo_before={it['photo_before_timestamp'][11:]}  "
                  f"interest=[{it['interest_start'][11:]} .. {it['interest_end'][11:]}]  "
                  f"photo_after={it['photo_after_timestamp'][11:]}  "
                  f"cargo={it['cargo_type']}  geo={it.get('geo')}  fires={it['fires_count']}")
            for a in alarms:
                print(f"     alarm IO_{a['io']}  {a['bt'][11:]}–{a['et'][11:]}  "
                      f"({a['dur']:.0f}s, ssp={a['ssp']}, esp={a['esp']})  ps={a['lat_lng_start']}")

        for fn in c["false_negatives"]:
            print(f"\n[FN] контрактор: {fn['start'][11:]}–{fn['end'][11:]}  "
                  f"{fn['type']} cnt={fn['count']}")
            # Загляним в окно ±2 мин: что было в алармах?
            try:
                # FN stored as string after _make_serializable
                if isinstance(fn['start'], str):
                    s_dt = _parse_dt(fn['start'])
                    e_dt = _parse_dt(fn['end'])
                else:
                    s_dt = fn['start']; e_dt = fn['end']
            except Exception:
                continue
            alarms = _alarms_in(plate, date, s_dt - dt.timedelta(seconds=120), e_dt + dt.timedelta(seconds=120))
            if alarms:
                print(f"     алармы в окне ±2 мин:")
                for a in alarms:
                    print(f"       IO_{a['io']}  {a['bt'][11:]}–{a['et'][11:]}  "
                          f"({a['dur']:.0f}s, ssp={a['ssp']}, esp={a['esp']})  ps={a['lat_lng_start']}")
            else:
                print(f"     алармов в окне ±2 мин нет — сенсор не сработал, "
                      f"либо все сработки попали в монстро-аларм (фильтр алгоритма).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
