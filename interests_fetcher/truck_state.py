"""
Состояние траков в states.json: trucks[uuid] + truck_info (профиль из cms_gate).
Внешний идентификатор пайплайна — reg_id (mdvr / DevIDNO).
"""
from __future__ import annotations

import datetime
import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from interests_fetcher.logger import logger


def _cms_base_url() -> str:
    return os.environ.get("CMS_GATE_BASE_URL", "http://localhost:8081/api/v1")


def _cms_token() -> str:
    return os.environ.get("CMS_GATE_API_TOKEN", "")


def _auth_headers() -> Dict[str, str]:
    t = _cms_token()
    return {"Authorization": f"Bearer {t}"} if t else {}


def default_local_truck_record() -> Dict[str, Any]:
    """Локальные поля записи трака (без профиля ТС из gate)."""
    last_upload = datetime.datetime.today() - datetime.timedelta(days=7)
    last_upload_str = last_upload.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "ignore": False,
        "interests": [],
        "last_upload_time": last_upload_str,
        "by_trigger": 1,
        "by_stops": 0,
        "by_door_limit_switch": 0,
        "by_lifting_limit_switch": 1,
        "continuous": 0,
        "pending_interests": [],
        "gaps": [],
        "truck_info": {},
    }


def truck_info_from_api_truck(api_row: Dict[str, Any]) -> Dict[str, Any]:
    """Собирает truck_info из ответа GET /trucks (TruckResponse)."""
    mdvr = api_row.get("mdvr") or {}
    plate = api_row.get("plate")
    ti = {
        "model": mdvr.get("model") or "",
        "id": mdvr.get("id"),
        "main_channel": int(mdvr.get("main_channel", 0)),
        "euro_container_alarm": int(mdvr.get("euro_container_alarm", 0)),
        "kgo_container_alarm": int(mdvr.get("kgo_container_alarm", 0)),
        "euro_container_alarm_type": str(mdvr.get("euro_container_alarm_type") or ""),
        "kgo_container_alarm_type": str(mdvr.get("kgo_container_alarm_type") or ""),
        "plate": plate,
    }
    return ti


def truck_info_from_legacy_flat_reg(old: Dict[str, Any], reg_id: str) -> Dict[str, Any]:
    """Миграция старого плоского regs[reg_id] в truck_info."""
    return {
        "model": old.get("model") or "",
        "id": reg_id,
        "main_channel": int(old.get("chanel_id", 0)),
        "euro_container_alarm": int(old.get("euro_container_alarm", 4)),
        "kgo_container_alarm": int(old.get("kgo_container_alarm", 3)),
        "euro_container_alarm_type": str(old.get("euro_container_alarm_type") or ""),
        "kgo_container_alarm_type": str(old.get("kgo_container_alarm_type") or ""),
        "plate": old.get("plate"),
    }


def find_truck_id_by_mdvr(states: Dict[str, Any], reg_id: str) -> Optional[str]:
    trucks = states.get("trucks") or {}
    for tid, row in trucks.items():
        ti = row.get("truck_info") or {}
        if ti.get("id") == reg_id:
            return str(tid)
    return None


def flatten_truck_for_pipeline(truck: Dict[str, Any]) -> Dict[str, Any]:
    """
    Плоский dict как у старого regs[reg_id] для main_operator / cms_interface / api.
    """
    ti = truck.get("truck_info") or {}
    flat = {k: v for k, v in truck.items() if k != "truck_info"}
    flat["plate"] = ti.get("plate")
    flat["chanel_id"] = ti.get("main_channel", 0)
    flat["euro_container_alarm"] = ti.get("euro_container_alarm", 4)
    flat["kgo_container_alarm"] = ti.get("kgo_container_alarm", 3)
    if ti.get("euro_container_alarm_type") is not None:
        flat["euro_container_alarm_type"] = ti.get("euro_container_alarm_type")
    if ti.get("kgo_container_alarm_type") is not None:
        flat["kgo_container_alarm_type"] = ti.get("kgo_container_alarm_type")
    return json.loads(json.dumps(flat))


def ensure_truck_info_defaults(truck: Dict[str, Any]) -> bool:
    """Дополняет truck_info дефолтами алармов. Возвращает True если меняли."""
    ti = truck.setdefault("truck_info", {})
    changed = False
    if "euro_container_alarm" not in ti:
        ti["euro_container_alarm"] = 4
        changed = True
    if "kgo_container_alarm" not in ti:
        ti["kgo_container_alarm"] = 3
        changed = True
    if "main_channel" not in ti:
        ti["main_channel"] = 0
        changed = True
    if "model" not in ti:
        ti["model"] = ""
        changed = True
    if "euro_container_alarm_type" not in ti:
        ti["euro_container_alarm_type"] = ""
        changed = True
    if "kgo_container_alarm_type" not in ti:
        ti["kgo_container_alarm_type"] = ""
        changed = True
    return changed


def ensure_truck_structure_inplace(truck: Dict[str, Any]) -> bool:
    ch = ensure_truck_info_defaults(truck)
    if "pending_interests" not in truck:
        truck["pending_interests"] = []
        ch = True
    if "ignore" not in truck:
        truck["ignore"] = False
        ch = True
    if "interests" not in truck:
        truck["interests"] = []
        ch = True
    if "gaps" not in truck:
        truck["gaps"] = []
        ch = True
    for legacy_key in ("verified_until", "verified_until_long"):
        if legacy_key in truck:
            del truck[legacy_key]
            ch = True
    return ch


def merge_truck_info_from_api(into_truck: Dict[str, Any], api_row: Dict[str, Any]) -> None:
    """Обновляет только truck_info из ответа API."""
    into_truck["truck_info"] = truck_info_from_api_truck(api_row)


def merge_local_truck_state_prefer_canonical(into: Dict[str, Any], orphan: Dict[str, Any]) -> None:
    """
    Переносит локальное состояние из дубликата (тот же mdvr, другой truck_id)
    в каноническую запись. truck_info не трогаем — задаётся отдельно из API.
    """
    if not orphan:
        return
    for k in (
        "ignore",
        "interests",
        "last_upload_time",
        "by_trigger",
        "by_stops",
        "by_door_limit_switch",
        "by_lifting_limit_switch",
        "continuous",
        "processed_interests",
    ):
        if k in orphan:
            into[k] = orphan[k]
    p_into = list(into.get("pending_interests") or [])
    p_old = list(orphan.get("pending_interests") or [])
    seen = {x.get("name") for x in p_into if isinstance(x, dict)}
    for x in p_old:
        if isinstance(x, dict) and x.get("name") and x.get("name") not in seen:
            p_into.append(x)
            seen.add(x.get("name"))
    into["pending_interests"] = p_into

    g_into = list(into.get("gaps") or [])
    g_old = list(orphan.get("gaps") or [])
    seen_ids = {g.get("id") for g in g_into if isinstance(g, dict)}
    for g in g_old:
        if isinstance(g, dict) and g.get("id") and g.get("id") not in seen_ids:
            g_into.append(g)
            seen_ids.add(g.get("id"))
    into["gaps"] = g_into


def ensure_truck_row_for_mdvr(
    states: Dict[str, Any],
    reg_id: str,
    *,
    plate: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], bool]:
    """
    Находит или создаёт запись trucks[uuid] для данного mdvr.
    Возвращает (truck_id, row, created).
    """
    trucks = states.setdefault("trucks", {})
    tid = find_truck_id_by_mdvr(states, reg_id)
    if tid:
        row = trucks[tid]
        if plate is not None:
            ti = row.setdefault("truck_info", {})
            if ti.get("plate") in (None, ""):
                ti["plate"] = plate
        return tid, row, False

    api = fetch_truck_by_mdvr_sync(reg_id)
    if api and api.get("truck_id"):
        tid = str(api.get("truck_id"))
        row = default_local_truck_record()
        merge_truck_info_from_api(row, api)
        if plate is not None:
            row.setdefault("truck_info", {})["plate"] = plate
        trucks[tid] = row
        return tid, row, True

    tid = str(uuid.uuid4())
    row = default_local_truck_record()
    ti = {
        "model": "",
        "id": reg_id,
        "main_channel": 0,
        "euro_container_alarm": 4,
        "kgo_container_alarm": 3,
        "euro_container_alarm_type": "",
        "kgo_container_alarm_type": "",
        "plate": plate,
    }
    row["truck_info"] = ti
    trucks[tid] = row
    return tid, row, True


def fetch_truck_by_mdvr_sync(mdvr_id: str, timeout_sec: float = 15.0) -> Optional[Dict[str, Any]]:
    """Синхронный GET /trucks/by-mdvr-id/{id} для вызова из sync-контекста."""
    from urllib.parse import quote

    safe = quote(mdvr_id, safe="")
    url = f"{_cms_base_url()}/trucks/by-mdvr-id/{safe}"
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.get(url, headers=_auth_headers())
    except Exception as e:
        logger.warning(f"[truck_state] fetch_truck_by_mdvr_sync transport error mdvr={mdvr_id}: {e}")
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.warning(
            f"[truck_state] fetch_truck_by_mdvr_sync HTTP {resp.status_code} mdvr={mdvr_id} body={resp.text[:500]}"
        )
        return None
    body = resp.json()
    return body if isinstance(body, dict) else None


def legacy_local_keys_from_old_reg(old: Dict[str, Any]) -> Dict[str, Any]:
    """Поля, которые переносим из старого regs в запись trucks (не идентификаторы ТС)."""
    skip = {
        "chanel_id",
        "plate",
        "euro_container_alarm",
        "kgo_container_alarm",
        "euro_container_alarm_type",
        "kgo_container_alarm_type",
        "model",
    }
    return {k: v for k, v in old.items() if k not in skip}


def migrate_regs_to_trucks_if_needed(states: Dict[str, Any]) -> bool:
    """
    Если есть legacy regs — переносим в trucks по данным API или из плоских полей.
    Возвращает True, если структура менялась.
    """
    regs = states.get("regs")
    if not isinstance(regs, dict) or not regs:
        return False

    trucks = states.setdefault("trucks", {})
    changed = False
    for reg_id, old in regs.items():
        if not isinstance(old, dict):
            continue
        if find_truck_id_by_mdvr(states, str(reg_id)):
            continue

        api_row = fetch_truck_by_mdvr_sync(str(reg_id))
        if api_row:
            tid = str(api_row.get("truck_id", "")).strip()
            if not tid:
                continue
            local = legacy_local_keys_from_old_reg(old)
            base = default_local_truck_record()
            base.update(local)
            base["truck_info"] = truck_info_from_api_truck(api_row)
            trucks[tid] = base
            changed = True
            logger.info(f"[truck_state] Миграция regs→trucks: mdvr={reg_id} truck_id={tid} (API)")
        else:
            tid = str(uuid.uuid4())
            local = legacy_local_keys_from_old_reg(old)
            base = default_local_truck_record()
            base.update(local)
            base["truck_info"] = truck_info_from_legacy_flat_reg(old, str(reg_id))
            trucks[tid] = base
            changed = True
            logger.warning(
                f"[truck_state] Миграция regs→trucks: mdvr={reg_id} truck_id={tid} без API — truck_info из локальных полей"
            )

    if "regs" in states:
        del states["regs"]
        changed = True
    return changed


def consolidate_trucks_by_mdvr(states: Dict[str, Any]) -> bool:
    """
    Сливает дубликаты с одинаковым truck_info.id в один (по первому truck_id).
    """
    trucks = states.get("trucks")
    if not isinstance(trucks, dict):
        return False
    by_mdvr: Dict[str, str] = {}
    to_del: List[str] = []
    changed = False
    for tid, row in list(trucks.items()):
        ti = (row or {}).get("truck_info") or {}
        mid = ti.get("id")
        if not mid:
            continue
        mid_s = str(mid)
        if mid_s not in by_mdvr:
            by_mdvr[mid_s] = str(tid)
            continue
        keeper = by_mdvr[mid_s]
        krow = trucks.get(keeper) or {}
        pending_k = list(krow.get("pending_interests") or [])
        pending_o = list((row or {}).get("pending_interests") or [])
        seen = {p.get("name") for p in pending_k if isinstance(p, dict)}
        for p in pending_o:
            if isinstance(p, dict) and p.get("name") and p.get("name") not in seen:
                pending_k.append(p)
                seen.add(p.get("name"))
        krow["pending_interests"] = pending_k

        gaps_k = list(krow.get("gaps") or [])
        gaps_o = list((row or {}).get("gaps") or [])
        seen_g = {g.get("id") for g in gaps_k if isinstance(g, dict)}
        for g in gaps_o:
            if isinstance(g, dict) and g.get("id") and g.get("id") not in seen_g:
                gaps_k.append(g)
                seen_g.add(g.get("id"))
        krow["gaps"] = gaps_k

        to_del.append(str(tid))
        changed = True
    for d in to_del:
        if d in trucks:
            del trucks[d]
    return changed
