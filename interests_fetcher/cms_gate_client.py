from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

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


async def list_devices(status: str = "all", timeout_sec: float = 180.0) -> List[Dict[str, Any]]:
    """
    Список устройств: GET /devices (синхронный ответ, без очереди).
    """
    client = _get_client()
    normalized_status = (status or "all").lower()
    url = f"{_get_base_url()}/devices"
    resp = await client.get(
        url,
        params={"status": normalized_status},
        headers=_auth_headers(),
        timeout=timeout_sec,
    )
    if resp.status_code >= 400:
        logger.error(
            f"[cms_gate_client] list_devices failed status={resp.status_code} body={resp.text}"
        )
        resp.raise_for_status()
    body = resp.json()
    devices = body.get("devices") or []
    if not isinstance(devices, list):
        raise RuntimeError(f"Unexpected devices type from cms_gate: {type(devices)}")
    return devices  # type: ignore[return-value]


async def get_tracks_and_alarms(
    reg_id: str,
    start_time: str,
    end_time: str,
    timeout_sec: float = 300.0,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Треки и алармы: GET /tracks-alarms (синхронный ответ).
    """
    client = _get_client()
    url = f"{_get_base_url()}/tracks-alarms"
    resp = await client.get(
        url,
        params={"reg_id": reg_id, "start_time": start_time, "end_time": end_time},
        headers=_auth_headers(),
        timeout=timeout_sec,
    )
    if resp.status_code >= 400:
        logger.error(
            f"[cms_gate_client] tracks-alarms failed status={resp.status_code} body={resp.text}"
        )
        resp.raise_for_status()
    res = resp.json()
    tracks = res.get("tracks") or []
    alarms = res.get("alarms") or []
    if not isinstance(tracks, list) or not isinstance(alarms, list):
        raise RuntimeError(
            f"Unexpected tracks/alarms type from cms_gate: {type(tracks)} / {type(alarms)}"
        )
    return tracks, alarms  # type: ignore[return-value]


async def get_device_status(reg_id: str) -> Dict[str, Any]:
    """
    Статус устройства: GET /devices/{reg_id}/status.
    """
    client = _get_client()
    url = f"{_get_base_url()}/devices/{reg_id}/status"
    resp = await client.get(url, headers=_auth_headers())
    if resp.status_code >= 400:
        logger.error(
            f"[cms_gate_client] device_status failed status={resp.status_code} body={resp.text}"
        )
        resp.raise_for_status()
    body = resp.json()
    device = body.get("device") or {}
    if not isinstance(device, dict):
        raise RuntimeError(f"Unexpected device type from cms_gate: {type(device)}")
    return device  # type: ignore[return-value]


async def download_clips_for_interest(
    reg_id: str,
    interest: Dict[str, Any],
    channels: Optional[List[int]] = None,
    timeout_sec: float = 1800.0,
) -> Dict[int, Dict[str, Any]]:
    """
    Скачивание клипов: POST /download-clips-for-interest (синхронный ответ).
    """
    client = _get_client()
    url = f"{_get_base_url()}/download-clips-for-interest"
    payload: Dict[str, Any] = {"reg_id": reg_id, "interest": interest}
    if channels is not None:
        payload["channels"] = channels

    resp = await client.post(
        url,
        json=payload,
        headers=_auth_headers(),
        timeout=timeout_sec,
    )
    if resp.status_code >= 400:
        logger.error(
            f"[cms_gate_client] download_clips_for_interest failed "
            f"status={resp.status_code} body={resp.text}"
        )
        resp.raise_for_status()
    res = resp.json()
    channels_map = res.get("channels") or {}
    if not isinstance(channels_map, dict):
        raise RuntimeError(f"Unexpected channels type from cms_gate: {type(channels_map)}")

    normalized: Dict[int, Dict[str, Any]] = {}
    for k, v in channels_map.items():
        try:
            ch = int(k)
        except Exception:
            if isinstance(k, int):
                ch = k
            else:
                continue
        if isinstance(v, dict):
            normalized[ch] = v

    return normalized
