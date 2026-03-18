from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from interests_fetcher.logger import logger


_client: Optional[httpx.AsyncClient] = None


def _get_base_url() -> str:
    """
    Базовый URL cms_gate.

    Берётся из переменной окружения CMS_GATE_BASE_URL, либо по умолчанию
    считается, что сервис доступен на localhost:9000.
    """

    return os.environ.get("CMS_GATE_BASE_URL", "http://localhost:9000/api/v1")


def _get_api_token() -> str:
    """
    Токен для авторизации в cms_gate.

    Совпадает с CMS_GATE_API_TOKEN, который использует сам сервис cms_gate.
    """

    return os.environ.get("CMS_GATE_API_TOKEN", "")


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _auth_headers() -> Dict[str, str]:
    token = _get_api_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def submit_job(job_type: str, payload: Dict[str, Any]) -> str:
    """
    Создаёт задачу в cms_gate и возвращает job_id.
    """

    client = _get_client()
    url = f"{_get_base_url()}/jobs/{job_type}"
    logger.debug(f"[cms_gate_client] submit_job {job_type} -> {url}")
    resp = await client.post(url, json=payload, headers=_auth_headers())
    if resp.status_code >= 400:
        logger.error(
            f"[cms_gate_client] submit_job {job_type} failed "
            f"status={resp.status_code} body={resp.text}"
        )
        resp.raise_for_status()
    data = resp.json()
    job_id = data.get("job_id")
    if not job_id:
        raise RuntimeError(f"cms_gate returned no job_id for job_type={job_type}")
    return job_id


async def wait_for_job(
    job_id: str,
    timeout_sec: float = 60.0,
    poll_interval_sec: float = 1.0,
) -> Dict[str, Any]:
    """
    Ожидает завершения задачи cms_gate с данным job_id.

    Возвращает объект result из ответа cms_gate, либо бросает исключение при ошибке/таймауте.
    """

    client = _get_client()
    url = f"{_get_base_url()}/jobs/{job_id}"
    loop = asyncio.get_running_loop()
    end_ts = loop.time() + timeout_sec
    last_status: str | None = None

    while True:
        resp = await client.get(url, headers=_auth_headers())
        if resp.status_code >= 400:
            logger.error(
                f"[cms_gate_client] wait_for_job {job_id} failed "
                f"status={resp.status_code} body={resp.text}"
            )
            resp.raise_for_status()

        body = resp.json()
        status = body.get("status")
        result = body.get("result")
        error = body.get("error")
        last_status = status

        if status in ("success", "failure"):
            if status == "failure":
                raise RuntimeError(f"cms_gate job {job_id} failed: {error}")
            if not isinstance(result, dict):
                raise RuntimeError(f"cms_gate job {job_id} returned non-dict result: {result!r}")
            return result

        if loop.time() >= end_ts:
            raise TimeoutError(
                f"Timeout waiting for cms_gate job {job_id}; "
                f"last_status={last_status!r}; timeout_sec={timeout_sec}"
            )

        await asyncio.sleep(poll_interval_sec)


async def list_devices(status: str = "all", timeout_sec: float = 180.0) -> List[Dict[str, Any]]:
    """
    Получает список устройств напрямую через GET /devices (без Celery job).
    Если прямой вызов недоступен (старый cms_gate), делает fallback на job API.
    """
    client = _get_client()
    normalized_status = (status or "all").lower()
    url = f"{_get_base_url()}/devices"
    try:
        resp = await client.get(
            url,
            params={"status": normalized_status},
            headers=_auth_headers(),
            timeout=timeout_sec,
        )
        if resp.status_code < 400:
            body = resp.json()
            devices = body.get("devices") or []
            if not isinstance(devices, list):
                raise RuntimeError(f"Unexpected devices type from cms_gate direct API: {type(devices)}")
            return devices  # type: ignore[return-value]
        logger.warning(
            f"[cms_gate_client] direct list_devices failed status={resp.status_code}; "
            f"fallback to job API"
        )
    except Exception as e:
        logger.warning(f"[cms_gate_client] direct list_devices request error: {e}; fallback to job API")

    job_id = await submit_job("list_devices", {"status": normalized_status})
    res = await wait_for_job(job_id, timeout_sec=timeout_sec)
    devices = res.get("devices") or []
    if not isinstance(devices, list):
        raise RuntimeError(f"Unexpected devices type from cms_gate job API: {type(devices)}")
    return devices  # type: ignore[return-value]


async def get_tracks_and_alarms(
    reg_id: str,
    start_time: str,
    end_time: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Обёртка над job_type=tracks_alarms.
    Возвращает (tracks, alarms).
    """

    job_id = await submit_job(
        "tracks_alarms",
        {"reg_id": reg_id, "start_time": start_time, "end_time": end_time},
    )
    res = await wait_for_job(job_id)
    tracks = res.get("tracks") or []
    alarms = res.get("alarms") or []
    if not isinstance(tracks, list) or not isinstance(alarms, list):
        raise RuntimeError(
            f"Unexpected tracks/alarms type from cms_gate: "
            f"{type(tracks)} / {type(alarms)}"
        )
    return tracks, alarms  # type: ignore[return-value]


async def get_device_status(reg_id: str) -> Dict[str, Any]:
    """
    Получает статус устройства напрямую через GET /devices/{reg_id}/status.
    Если прямой вызов недоступен (старый cms_gate), делает fallback на job API.
    """
    client = _get_client()
    url = f"{_get_base_url()}/devices/{reg_id}/status"
    try:
        resp = await client.get(url, headers=_auth_headers())
        if resp.status_code < 400:
            body = resp.json()
            device = body.get("device") or {}
            if not isinstance(device, dict):
                raise RuntimeError(f"Unexpected device type from cms_gate direct API: {type(device)}")
            return device  # type: ignore[return-value]
        logger.warning(
            f"[cms_gate_client] direct device_status failed status={resp.status_code}; "
            f"fallback to job API"
        )
    except Exception as e:
        logger.warning(f"[cms_gate_client] direct device_status request error: {e}; fallback to job API")

    job_id = await submit_job("device_status", {"reg_id": reg_id})
    res = await wait_for_job(job_id)
    device = res.get("device") or {}
    if not isinstance(device, dict):
        raise RuntimeError(f"Unexpected device type from cms_gate job API: {type(device)}")
    return device  # type: ignore[return-value]


async def download_clips_for_interest(
    reg_id: str,
    interest: Dict[str, Any],
    channels: Optional[List[int]] = None,
    timeout_sec: float = 600.0,
) -> Dict[int, Dict[str, Any]]:
    """
    Обёртка над job_type=download_clips_for_interest.

    Возвращает словарь вида:
      {ch: {"path": str|None, "concat_sources": list[str]|None}, ...}
    """

    payload: Dict[str, Any] = {
        "reg_id": reg_id,
        "interest": interest,
    }
    if channels is not None:
        payload["channels"] = channels

    job_id = await submit_job("download_clips_for_interest", payload)
    res = await wait_for_job(job_id, timeout_sec=timeout_sec, poll_interval_sec=2.0)
    channels_map = res.get("channels") or {}
    if not isinstance(channels_map, dict):
        raise RuntimeError(f"Unexpected channels type from cms_gate: {type(channels_map)}")

    # Ключи приходят как строки; приводим к int, если возможно.
    normalized: Dict[int, Dict[str, Any]] = {}
    for k, v in channels_map.items():
        try:
            ch = int(k)
        except Exception:
            # если ключ уже int или странный — пробуем использовать как есть
            if isinstance(k, int):
                ch = k
            else:
                continue
        if isinstance(v, dict):
            normalized[ch] = v

    return normalized

