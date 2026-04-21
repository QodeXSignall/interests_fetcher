"""
Sanity-тесты для gap-механики (этапы 1/2/2.5/3/4) на синтетике.

Изолируем states.json через временный файл, чтобы не трогать боевой:
 - подменяем `settings.states`
 - подменяем `filelocker.STATES_PATH` / `LOCK_PATH`
 - подменяем `functions.LOCK_PATH` (локальный bind при import)

Запуск:
    .venv/bin/python misc/sanity_tests.py
"""

from __future__ import annotations

import os
import sys
import json
import tempfile
import datetime
import traceback
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _isolate_states() -> str:
    tmpdir = tempfile.mkdtemp(prefix="sanity_states_")
    tmp_states = os.path.join(tmpdir, "states.json")
    with open(tmp_states, "w", encoding="utf-8") as f:
        json.dump({"trucks": {}}, f)

    from interests_fetcher.data import settings as _settings
    _settings.states = tmp_states

    from interests_fetcher import filelocker as _fl
    _fl.STATES_PATH = tmp_states
    _fl.LOCK_PATH = tmp_states + ".lock"

    from interests_fetcher import functions as _func
    _func.LOCK_PATH = tmp_states + ".lock"

    return tmp_states


STATES_PATH = _isolate_states()
print(f"[sanity] isolated states.json: {STATES_PATH}\n")

from interests_fetcher.data import settings as _settings  # noqa: E402
from interests_fetcher import functions as main_funcs  # noqa: E402
from interests_fetcher.cms_interface import functions as cms_api_funcs  # noqa: E402
from interests_fetcher import truck_state as ts  # noqa: E402

TIME_FMT = _settings.TIME_FMT

REG = "SANITY_REG"


def _now_str(offset_min: int = 0) -> str:
    return (datetime.datetime.now() + datetime.timedelta(minutes=offset_min)).strftime(TIME_FMT)


def _seed_reg(reg_id: str = REG, plate: str = "SAN000") -> None:
    """Создаём минимальную truck-строку под нашим reg_id."""
    from interests_fetcher.filelocker import _load_states, _atomic_save_states, LOCK_PATH, FileLock
    with FileLock(LOCK_PATH):
        states = _load_states()
        trucks = states.setdefault("trucks", {})
        trucks[reg_id] = {
            "mdvr_serial": reg_id,
            "truck_info": {
                "id": reg_id,
                "plate": plate,
                "mdvr": {"id": reg_id},
            },
            "plate": plate,
            "pending_interests": [],
            "gaps": [],
        }
        _atomic_save_states(states)


def _reset_gaps(reg_id: str = REG) -> None:
    from interests_fetcher.filelocker import _load_states, _atomic_save_states, LOCK_PATH, FileLock
    with FileLock(LOCK_PATH):
        states = _load_states()
        if reg_id in states["trucks"]:
            states["trucks"][reg_id]["gaps"] = []
            states["trucks"][reg_id]["pending_interests"] = []
            _atomic_save_states(states)


def _dump_gaps(reg_id: str = REG) -> list[dict]:
    return main_funcs.get_gaps(reg_id)


# -----------------------------------------------------------------------------
# runner
# -----------------------------------------------------------------------------

_passed = 0
_failed = 0
_failures: list[str] = []


def run_test(name: str, fn):
    global _passed, _failed
    print(f"[test] {name} ... ", end="", flush=True)
    try:
        _reset_gaps()
        fn()
        print("OK")
        _passed += 1
    except AssertionError as e:
        print(f"FAIL: {e}")
        _failed += 1
        _failures.append(f"{name}: {e}")
        traceback.print_exc()
    except Exception as e:
        print(f"ERROR: {e!r}")
        _failed += 1
        _failures.append(f"{name}: {e!r}")
        traceback.print_exc()


# =============================================================================
# 1) detect_gaps
# =============================================================================

def _track(gt: str, ls: int = 0, es: int = 0) -> dict:
    return {"gt": gt, "ls": ls, "es": es}


def test_detect_gaps_basic():
    # два трека с дыркой 5 минут -> gap >= 120s
    tracks = [
        _track("2026-04-13 10:00:00"),
        _track("2026-04-13 10:05:00"),
        _track("2026-04-13 10:05:15"),
    ]
    g = cms_api_funcs.detect_gaps(
        tracks=tracks,
        window_start="2026-04-13 09:00:00",
        window_end="2026-04-13 11:00:00",
        reg_id=REG,
    )
    assert len(g) == 1, f"expected 1 gap, got {g}"
    assert g[0]["gap_start"] == "2026-04-13 10:00:00"
    assert g[0]["gap_end"] == "2026-04-13 10:05:00"


def test_detect_gaps_below_threshold():
    # дырка 60с (< 120s) -> не gap
    tracks = [
        _track("2026-04-13 10:00:00"),
        _track("2026-04-13 10:01:00"),
    ]
    g = cms_api_funcs.detect_gaps(
        tracks=tracks,
        window_start="2026-04-13 09:00:00",
        window_end="2026-04-13 11:00:00",
        reg_id=REG,
    )
    assert g == [], f"expected no gaps, got {g}"


def test_detect_gaps_ignore_stationary():
    # оба конца стоянка (ls=4, es=1) -> игнорируем
    tracks = [
        _track("2026-04-13 10:00:00", ls=4, es=1),
        _track("2026-04-13 10:10:00", ls=4, es=1),
    ]
    g = cms_api_funcs.detect_gaps(
        tracks=tracks,
        window_start="2026-04-13 09:00:00",
        window_end="2026-04-13 11:00:00",
        reg_id=REG,
    )
    assert g == [], f"expected ignored (stationary), got {g}"


def test_detect_gaps_half_stationary():
    # один конец — движение (ls!=4) -> считаем gap
    tracks = [
        _track("2026-04-13 10:00:00", ls=4, es=1),
        _track("2026-04-13 10:10:00", ls=0, es=0),
    ]
    g = cms_api_funcs.detect_gaps(
        tracks=tracks,
        window_start="2026-04-13 09:00:00",
        window_end="2026-04-13 11:00:00",
        reg_id=REG,
    )
    assert len(g) == 1, f"expected 1 gap, got {g}"


def test_detect_gaps_edge_ignore():
    # трек вне окна отсекается -> оставшихся < 2 -> 0 gap'ов
    tracks = [
        _track("2026-04-13 08:00:00"),  # вне окна слева
        _track("2026-04-13 10:10:00"),  # единственный в окне
    ]
    g = cms_api_funcs.detect_gaps(
        tracks=tracks,
        window_start="2026-04-13 09:00:00",
        window_end="2026-04-13 11:00:00",
        reg_id=REG,
    )
    assert g == [], f"expected no gaps (edge_ignore), got {g}"


# =============================================================================
# 2) reconcile_gaps_in_window
# =============================================================================

def _upsert(gap_start: str, gap_end: str, linked=None) -> str:
    gid = main_funcs.upsert_gap(REG, gap_start, gap_end, linked_interest_names=linked)
    assert gid, "upsert_gap returned None"
    return gid


def test_reconcile_closed():
    gid = _upsert("2026-04-13 10:00:00", "2026-04-13 10:05:00")
    res = main_funcs.reconcile_gaps_in_window(
        reg_id=REG,
        detected_gaps=[],
        window_start="2026-04-13 09:30:00",
        window_end="2026-04-13 10:30:00",
        points_per_gap={gid: 5},
        stable_checks_threshold=3,
    )
    assert gid in res["closed"], f"expected closed=[{gid}], got {res}"
    gaps = _dump_gaps()
    g = next(g for g in gaps if g["id"] == gid)
    assert g["status"] == "closed"
    assert g.get("closed_at"), "closed_at missing"


def test_reconcile_no_change_and_no_progress():
    # threshold=1 -> после одного «застоя» сразу в no_progress
    gid = _upsert("2026-04-13 10:00:00", "2026-04-13 10:05:00")
    # предварительно пометим, что видели 3 точки — и дальше подтверждаем снова 3
    main_funcs.update_gap(REG, gid, points_last_seen=3)

    res = main_funcs.reconcile_gaps_in_window(
        reg_id=REG,
        detected_gaps=[{
            "gap_start": "2026-04-13 10:00:00",
            "gap_end": "2026-04-13 10:05:00",
        }],
        window_start="2026-04-13 09:30:00",
        window_end="2026-04-13 10:30:00",
        points_per_gap={gid: 3},
        stable_checks_threshold=1,
    )
    assert gid in res["updated"]
    assert gid in res["no_progress"], f"expected no_progress=[{gid}], got {res}"
    g = next(g for g in _dump_gaps() if g["id"] == gid)
    assert g["checked"] == 1
    assert g["last_checked"]
    assert g["no_progress_streak"] >= 1


def test_reconcile_shrunk():
    gid = _upsert("2026-04-13 10:00:00", "2026-04-13 10:10:00")
    main_funcs.update_gap(REG, gid, checked=5, no_progress_streak=2, points_last_seen=7)
    res = main_funcs.reconcile_gaps_in_window(
        reg_id=REG,
        detected_gaps=[{
            "gap_start": "2026-04-13 10:03:00",
            "gap_end": "2026-04-13 10:07:00",
        }],
        window_start="2026-04-13 09:30:00",
        window_end="2026-04-13 10:30:00",
        points_per_gap={gid: 9},
        stable_checks_threshold=3,
    )
    assert gid in res["updated"]
    assert gid not in res["no_progress"]
    g = next(g for g in _dump_gaps() if g["id"] == gid)
    assert g["gap_start"] == "2026-04-13 10:03:00"
    assert g["gap_end"] == "2026-04-13 10:07:00"
    assert g["checked"] == 0, f"expected checked reset, got {g['checked']}"
    assert g["no_progress_streak"] == 0


def test_reconcile_split():
    # один старый gap, два detected внутри -> SPLIT
    gid = _upsert("2026-04-13 10:00:00", "2026-04-13 10:20:00")
    created_at_orig = next(g for g in _dump_gaps() if g["id"] == gid)["created_at"]
    res = main_funcs.reconcile_gaps_in_window(
        reg_id=REG,
        detected_gaps=[
            {"gap_start": "2026-04-13 10:01:00", "gap_end": "2026-04-13 10:05:00"},
            {"gap_start": "2026-04-13 10:12:00", "gap_end": "2026-04-13 10:18:00"},
        ],
        window_start="2026-04-13 09:30:00",
        window_end="2026-04-13 10:30:00",
        points_per_gap={gid: 4},
        stable_checks_threshold=3,
    )
    all_gaps = [g for g in _dump_gaps() if g["status"] == "pending"]
    # исходный gid обновлён до первого интервала, появился второй с тем же created_at
    assert len(all_gaps) == 2, f"expected 2 pending gaps after split, got {all_gaps}"
    orig = next((g for g in all_gaps if g["id"] == gid), None)
    new = next((g for g in all_gaps if g["id"] != gid), None)
    assert orig is not None and new is not None
    assert orig["gap_start"] == "2026-04-13 10:01:00"
    assert orig["gap_end"] == "2026-04-13 10:05:00"
    assert new["gap_start"] == "2026-04-13 10:12:00"
    assert new["gap_end"] == "2026-04-13 10:18:00"
    assert new["created_at"] == created_at_orig, \
        f"split new gap must inherit created_at {created_at_orig}, got {new['created_at']}"


def test_reconcile_new():
    # никаких старых в окне -> новый gap создаётся
    res = main_funcs.reconcile_gaps_in_window(
        reg_id=REG,
        detected_gaps=[{
            "gap_start": "2026-04-13 10:00:00",
            "gap_end": "2026-04-13 10:05:00",
        }],
        window_start="2026-04-13 09:00:00",
        window_end="2026-04-13 11:00:00",
        stable_checks_threshold=3,
    )
    gaps = [g for g in _dump_gaps() if g["status"] == "pending"]
    assert len(gaps) == 1
    g = gaps[0]
    assert g["gap_start"] == "2026-04-13 10:00:00"
    assert g["gap_end"] == "2026-04-13 10:05:00"
    assert g["id"] in res["updated"]


def test_reconcile_closed_drops_blocking_ids():
    # gap закрывается -> blocking_gap_ids у pending_interests чистится
    gid = _upsert("2026-04-13 10:00:00", "2026-04-13 10:05:00")
    main_funcs.append_pending_interests(REG, [{
        "name": "blocked_it",
        "start_time": "2026-04-13 09:58:00",
        "end_time": "2026-04-13 10:07:00",
    }])
    main_funcs.set_blocking_gap_ids(REG, {"blocked_it": [gid]})
    pi_before = main_funcs.get_pending_interests(REG)
    assert pi_before[0]["blocking_gap_ids"] == [gid]

    main_funcs.reconcile_gaps_in_window(
        reg_id=REG,
        detected_gaps=[],
        window_start="2026-04-13 09:30:00",
        window_end="2026-04-13 10:30:00",
        stable_checks_threshold=3,
    )
    pi_after = main_funcs.get_pending_interests(REG)
    assert pi_after[0]["blocking_gap_ids"] == [], \
        f"blocking_gap_ids must be cleared after CLOSED, got {pi_after[0]['blocking_gap_ids']}"


# =============================================================================
# 3) gc_gaps
# =============================================================================

def test_gc_gaps_removes_expired():
    g1 = _upsert("2026-04-10 10:00:00", "2026-04-10 10:05:00")
    g2 = _upsert("2026-04-11 10:00:00", "2026-04-11 10:05:00")
    g3 = _upsert("2026-04-12 10:00:00", "2026-04-12 10:05:00")

    now = datetime.datetime.now()
    old = (now - datetime.timedelta(days=20)).strftime(TIME_FMT)
    recent = (now - datetime.timedelta(days=1)).strftime(TIME_FMT)

    main_funcs.update_gap(REG, g1, status="closed", closed_at=old)
    main_funcs.update_gap(REG, g2, status="abandoned", closed_at=old)
    main_funcs.update_gap(REG, g3, status="closed", closed_at=recent)

    res = main_funcs.gc_gaps(REG, ttl_closed_days=7, ttl_abandoned_days=30)
    # closed старше 7 -> удалён; abandoned старше 20 (<30) -> НЕ удалён; closed свежий -> НЕ удалён
    assert res == {"closed": 1, "abandoned": 0}, f"got {res}"

    gaps = _dump_gaps()
    ids = {g["id"] for g in gaps}
    assert g1 not in ids
    assert g2 in ids  # abandoned 20d < TTL 30d
    assert g3 in ids  # closed 1d < TTL 7d


def test_gc_gaps_ttl_zero_clears_all_terminal():
    g1 = _upsert("2026-04-10 10:00:00", "2026-04-10 10:05:00")
    g2 = _upsert("2026-04-11 10:00:00", "2026-04-11 10:05:00")
    now_str = _now_str()
    main_funcs.update_gap(REG, g1, status="closed", closed_at=now_str)
    main_funcs.update_gap(REG, g2, status="abandoned", closed_at=now_str)

    res = main_funcs.gc_gaps(REG, ttl_closed_days=0, ttl_abandoned_days=0)
    assert res == {"closed": 1, "abandoned": 1}, f"got {res}"
    assert _dump_gaps() == []


def test_gc_gaps_skips_pending_and_no_closed_at():
    g1 = _upsert("2026-04-10 10:00:00", "2026-04-10 10:05:00")  # pending
    g2 = _upsert("2026-04-11 10:00:00", "2026-04-11 10:05:00")
    # переведём во closed, но не поставим closed_at
    main_funcs.update_gap(REG, g2, status="closed")
    res = main_funcs.gc_gaps(REG, ttl_closed_days=0, ttl_abandoned_days=0)
    assert res == {"closed": 0, "abandoned": 0}
    ids = {g["id"] for g in _dump_gaps()}
    assert g1 in ids and g2 in ids


# =============================================================================
# 4) _build_fallback_interests_for_gap (статический метод, не требует сети)
# =============================================================================

def test_fallback_happy_path_and_filters():
    # создаём инстанс Main аккуратно — без login/побочек
    from main_operator import Main
    inst = Main.__new__(Main)
    inst.TIME_FMT = TIME_FMT  # у Main статический TIME_FMT, но подстрахуемся

    gap = {
        "id": "GAP-A",
        "gap_start": "2026-04-13 10:00:00",
        "gap_end": "2026-04-13 10:30:00",
    }
    reg_cfg = {"plate": "SAN000"}

    def _alarm(start, end, *, ssp_kmh=0.0, cargo="euro", start_stopped=True, io_index=22):
        a = {
            "start_str": start,
            "start_dt": datetime.datetime.strptime(start, TIME_FMT),
            "ssp_kmh": ssp_kmh,
            "cargo_type": cargo,
            "start_stopped": start_stopped,
            "io_index": io_index,
            "slng": None,
            "slat": None,
        }
        if end:
            a["end_str"] = end
            a["end_dt"] = datetime.datetime.strptime(end, TIME_FMT)
        return a

    prepared = {"alarms": [
        _alarm("2026-04-13 10:05:00", "2026-04-13 10:06:00"),          # OK
        _alarm("2026-04-13 10:06:15", "2026-04-13 10:06:45"),          # близко — должен смерджиться
        _alarm("2026-04-13 10:20:00", None),                            # без end — отдельный интерес
        _alarm("2026-04-13 10:11:00", None, ssp_kmh=5.0),              # быстрый -> skip
        _alarm("2026-04-13 10:12:00", None, start_stopped=False),      # не стоял -> skip
        _alarm("2026-04-13 10:13:00", None, cargo="unknown"),          # unknown -> skip
        _alarm("2026-04-13 09:30:00", None),                            # вне gap -> skip
    ]}

    fallbacks = inst._build_fallback_interests_for_gap(
        reg_id=REG, reg_cfg=reg_cfg, gap=gap, prepared_alarms=prepared,
    )
    # после фильтра и мерджа ожидаем 2 интереса:
    #  - cluster [10:05..10:06] + [10:06:15..10:06:45] (расстояние между окнами после раздвижки < 30с -> merge)
    #  - "10:20" без end -> отдельный
    assert len(fallbacks) == 2, f"expected 2 fallback interests, got {len(fallbacks)}: {fallbacks}"

    for it in fallbacks:
        assert it["reg_id"] == REG
        assert it["from_abandoned_gap"] is True
        assert it["fallback_gap_id"] == "GAP-A"
        assert it["source"] == "abandoned_gap_alarm_fallback"
        assert it["name"].startswith("SAN000_")

    # Проверим окно без end: start-2m .. start+5m
    noend = next(it for it in fallbacks if it["start_time"].endswith("10:18:00"))
    assert noend["end_time"].endswith("10:25:00"), f"noend end must be start+5m, got {noend['end_time']}"

    # Проверим кластер с end: start-2m .. end+2m, потом merge
    merged = next(it for it in fallbacks if it["start_time"].endswith("10:03:00"))
    # конец = 10:06:15 из 2-го аларма + 2m = 10:08:45
    assert merged["end_time"].endswith("10:08:45"), f"merged end wrong: {merged['end_time']}"
    assert merged["report"]["switches_amount"] == 2


def test_fallback_disabled_returns_empty():
    _settings.config.set("Interests", "GAP_ALARM_FALLBACK_ENABLED", "false")
    try:
        from main_operator import Main
        inst = Main.__new__(Main)
        inst.TIME_FMT = TIME_FMT
        gap = {
            "id": "GAP-B",
            "gap_start": "2026-04-13 10:00:00",
            "gap_end": "2026-04-13 10:30:00",
        }
        prepared = {"alarms": [{
            "start_str": "2026-04-13 10:05:00",
            "start_dt": datetime.datetime.strptime("2026-04-13 10:05:00", TIME_FMT),
            "end_str": "2026-04-13 10:06:00",
            "end_dt": datetime.datetime.strptime("2026-04-13 10:06:00", TIME_FMT),
            "ssp_kmh": 0.0,
            "cargo_type": "euro",
            "start_stopped": True,
            "io_index": 22,
        }]}
        res = inst._build_fallback_interests_for_gap(
            reg_id=REG, reg_cfg={"plate": "SAN000"}, gap=gap, prepared_alarms=prepared,
        )
        assert res == []
    finally:
        _settings.config.set("Interests", "GAP_ALARM_FALLBACK_ENABLED", "true")


# =============================================================================
# 5) pessimistic publication (мини-unit на предикате filter)
# =============================================================================

def test_pessimistic_filter_logic():
    """
    Воспроизводим фильтр из download_reg_videos: интересы с непустым
    blocking_gap_ids не проходят дальше.
    """
    pending = [
        {"name": "A", "blocking_gap_ids": []},
        {"name": "B", "blocking_gap_ids": ["gap1"]},
        {"name": "C"},  # отсутствует поле -> считаем unblocked
        {"name": "D", "blocking_gap_ids": None},
    ]
    unblocked = [it for it in pending if not (it.get("blocking_gap_ids") or [])]
    blocked = [it for it in pending if it.get("blocking_gap_ids")]
    assert [it["name"] for it in unblocked] == ["A", "C", "D"]
    assert [it["name"] for it in blocked] == ["B"]


# =============================================================================
# main
# =============================================================================

def main():
    _seed_reg()
    # предварительно проверим через truck_state, что регистратор найден
    tid = None
    from interests_fetcher.filelocker import _load_states
    states = _load_states()
    for t, r in states.get("trucks", {}).items():
        if r.get("mdvr_serial") == REG:
            tid = t
            break
    assert tid, "seed_reg не прошёл: нет нашей записи в states.json"

    tests = [
        ("detect_gaps.basic", test_detect_gaps_basic),
        ("detect_gaps.below_threshold", test_detect_gaps_below_threshold),
        ("detect_gaps.ignore_stationary", test_detect_gaps_ignore_stationary),
        ("detect_gaps.half_stationary", test_detect_gaps_half_stationary),
        ("detect_gaps.edge_ignore", test_detect_gaps_edge_ignore),
        ("reconcile.closed", test_reconcile_closed),
        ("reconcile.no_change_no_progress", test_reconcile_no_change_and_no_progress),
        ("reconcile.shrunk", test_reconcile_shrunk),
        ("reconcile.split", test_reconcile_split),
        ("reconcile.new", test_reconcile_new),
        ("reconcile.closed_drops_blocking_ids", test_reconcile_closed_drops_blocking_ids),
        ("gc_gaps.expired", test_gc_gaps_removes_expired),
        ("gc_gaps.ttl_zero", test_gc_gaps_ttl_zero_clears_all_terminal),
        ("gc_gaps.skip_no_closed_at", test_gc_gaps_skips_pending_and_no_closed_at),
        ("fallback.happy_path_and_filters", test_fallback_happy_path_and_filters),
        ("fallback.disabled", test_fallback_disabled_returns_empty),
        ("pessimistic.filter_logic", test_pessimistic_filter_logic),
    ]

    for name, fn in tests:
        run_test(name, fn)

    print(f"\n=== RESULT: {_passed} passed, {_failed} failed ===")
    if _failures:
        print("Failures:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
