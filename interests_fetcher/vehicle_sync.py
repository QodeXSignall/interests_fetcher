"""
Периодическая синхронизация truck_info из cms_gate GET /trucks (vehicle manager).
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict

from interests_fetcher import cms_gate_client
from interests_fetcher.data import settings as app_settings
from interests_fetcher.filelocker import LOCK_PATH, FileLock, _atomic_save_states, _load_states
from interests_fetcher.logger import logger
from interests_fetcher.truck_state import (
    consolidate_trucks_by_mdvr,
    default_local_truck_record,
    ensure_truck_structure_inplace,
    find_truck_id_by_mdvr,
    merge_local_truck_state_prefer_canonical,
    merge_truck_info_from_api,
)


def trucks_sync_interval_sec() -> float:
    env_v = os.environ.get("TRUCKS_SYNC_INTERVAL_SEC")
    if env_v:
        return max(60.0, float(env_v))
    try:
        if app_settings.config.has_section("VehicleSync"):
            return max(60.0, float(app_settings.config.getint("VehicleSync", "INTERVAL_SEC", fallback=900)))
    except Exception:
        pass
    return 900.0  # 15 min


async def sync_trucks_from_gate() -> Dict[str, Any]:
    """
    Подтягивает все траки с cms_gate и обновляет только truck_info в states.json.
    """
    t0 = time.perf_counter()
    raw = await cms_gate_client.list_all_trucks()
    entries_touched = 0
    with FileLock(LOCK_PATH):
        states = _load_states()
        trucks = states.setdefault("trucks", {})
        for row in raw:
            tid = str(row.get("truck_id", "")).strip()
            if not tid:
                continue
            mdvr_id = (row.get("mdvr") or {}).get("id")
            if mdvr_id:
                dup = find_truck_id_by_mdvr(states, str(mdvr_id))
                if dup and dup != tid and dup in trucks:
                    orphan = trucks.pop(dup)
                    cur = trucks.get(tid)
                    if cur is None:
                        cur = default_local_truck_record()
                        trucks[tid] = cur
                    merge_local_truck_state_prefer_canonical(cur, orphan)
            cur = trucks.get(tid)
            if cur is None:
                cur = default_local_truck_record()
                trucks[tid] = cur
            merge_truck_info_from_api(cur, row)
            ensure_truck_structure_inplace(cur)
            entries_touched += 1
        consolidate_trucks_by_mdvr(states)
        _atomic_save_states(states)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    out = {
        "trucks_seen": len(raw),
        "entries_touched": entries_touched,
        "duration_ms": round(elapsed_ms, 2),
    }
    logger.info("[vehicle_sync] sync_trucks_from_gate %s", out)
    return out


async def run_periodic_trucks_sync_after_delay(interval_sec: float) -> None:
    """Фон: sleep → sync → sleep → ... (первый sync делается отдельно при старте)."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await sync_trucks_from_gate()
        except Exception as e:
            logger.exception("[vehicle_sync] periodic sync failed: %s", e)
