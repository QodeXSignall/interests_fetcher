from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from interests_fetcher.data import settings as app_settings
from interests_fetcher.logger import logger, pipeline_event


_client: Optional[httpx.AsyncClient] = None


def _get_base_url() -> str:
    """
    Базовый URL cms_gate.

    Берётся из переменной окружения CMS_GATE_BASE_URL, либо по умолчанию
    считается, что сервис доступен на localhost:9000.
    """

    return os.environ.get("CMS_GATE_BASE_URL", "http://localhost:8081/api/v1")


def _download_clips_max_attempts() -> int:
    env_v = os.environ.get("CMS_GATE_DOWNLOAD_CLIPS_MAX_ATTEMPTS")
    if env_v:
        return max(1, int(env_v))
    try:
        return max(1, app_settings.config.getint("Interests", "DOWNLOAD_CLIPS_MAX_ATTEMPTS", fallback=5))
    except Exception:
        return 5


def _download_clips_retry_base_sec() -> float:
    env_v = os.environ.get("CMS_GATE_DOWNLOAD_CLIPS_RETRY_BASE_SEC")
    if env_v:
        return max(1.0, float(env_v))
    try:
        return max(
            1.0,
            float(app_settings.config.get("Interests", "DOWNLOAD_CLIPS_RETRY_DELAY_SEC", fallback="12")),
        )
    except Exception:
        return 12.0


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
    logger.debug(f"[cms_gate_client] GET {url} status={normalized_status}")
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
    logger.debug(
        f"[cms_gate_client] GET {url} reg_id={reg_id} start_time={start_time} end_time={end_time}"
    )
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
    logger.debug(f"[cms_gate_client] GET {url}")
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
    req_id = uuid.uuid4().hex[:12]
    interest_name = (interest or {}).get("name")
    t0 = time.perf_counter()
    logger.info(
        f"[cms_gate_client] req_id={req_id} POST {url} reg_id={reg_id} "
        f"interest={interest_name} channels={payload.get('channels')} timeout={timeout_sec}"
    )

    max_attempts = _download_clips_max_attempts()
    base_delay = _download_clips_retry_base_sec()
    last_err: Exception | None = None
    resp: httpx.Response | None = None
    final_http_attempt = 0

    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={**_auth_headers(), "X-Request-ID": f"{req_id}-a{attempt}"},
                timeout=timeout_sec,
            )
        except httpx.HTTPError as e:
            last_err = e
            elapsed = time.perf_counter() - t0
            logger.warning(
                f"[cms_gate_client] req_id={req_id} transport_error attempt={attempt}/{max_attempts} "
                f"reg_id={reg_id} interest={interest_name} elapsed={elapsed:.3f}s error={e!r}"
            )
            if attempt >= max_attempts:
                break
            delay = min(120.0, base_delay * (0.8 + 0.4 * attempt)) + random.uniform(0, 2)
            await asyncio.sleep(delay)
            continue

        assert resp is not None
        if resp.status_code in (429, 500, 502, 503, 504):
            last_err = None
            body_preview = (resp.text or "")[:500]
            logger.warning(
                f"[cms_gate_client] req_id={req_id} retryable HTTP {resp.status_code} "
                f"attempt={attempt}/{max_attempts} body={body_preview}"
            )
            if attempt >= max_attempts:
                break
            delay = min(120.0, base_delay * (0.8 + 0.4 * attempt)) + random.uniform(0, 2)
            await asyncio.sleep(delay)
            continue

        elapsed = time.perf_counter() - t0
        if resp.status_code >= 400:
            body_preview = (resp.text or "")[:2000]
            logger.error(
                f"[cms_gate_client] req_id={req_id} download_clips_for_interest failed "
                f"status={resp.status_code} elapsed={elapsed:.3f}s reg_id={reg_id} "
                f"interest={interest_name} channels={payload.get('channels')} body={body_preview}"
            )
            resp.raise_for_status()

        last_err = None
        final_http_attempt = attempt
        break

    if resp is None:
        elapsed = time.perf_counter() - t0
        logger.error(
            pipeline_event(
                "fetcher_http_download_clips_fail",
                req_id=req_id,
                reg_id=str(reg_id),
                interest=str(interest_name or ""),
                elapsed_sec=elapsed,
                reason="no_response",
            )
        )
        if last_err is not None:
            raise last_err
        raise RuntimeError("download_clips_for_interest: no response")

    if last_err is not None or resp.status_code in (429, 500, 502, 503, 504):
        elapsed = time.perf_counter() - t0
        body_preview = (resp.text or "")[:2000]
        logger.error(
            f"[cms_gate_client] req_id={req_id} download_clips_for_interest exhausted retries "
            f"status={resp.status_code} elapsed={elapsed:.3f}s reg_id={reg_id} "
            f"interest={interest_name} body={body_preview}"
        )
        logger.error(
            pipeline_event(
                "fetcher_http_download_clips_fail",
                req_id=req_id,
                reg_id=str(reg_id),
                interest=str(interest_name or ""),
                elapsed_sec=elapsed,
                http_status=resp.status_code,
            )
        )
        resp.raise_for_status()

    elapsed = time.perf_counter() - t0
    res = resp.json()
    channels_map = res.get("channels") or {}
    if not isinstance(channels_map, dict):
        raise RuntimeError(f"Unexpected channels type from cms_gate: {type(channels_map)}")
    logger.info(
        f"[cms_gate_client] req_id={req_id} success status={resp.status_code} elapsed={elapsed:.3f}s "
        f"reg_id={reg_id} interest={interest_name} channels_out={list(channels_map.keys())}"
    )

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

    paths_ok = sum(1 for v in normalized.values() if v.get("path"))
    n_req = len(channels) if channels is not None else len(normalized)
    logger.info(
        pipeline_event(
            "fetcher_http_download_clips_ok",
            req_id=req_id,
            reg_id=str(reg_id),
            interest=str(interest_name or ""),
            elapsed_sec=elapsed,
            http_attempt=final_http_attempt,
            channels_requested=n_req,
            paths_ok=paths_ok,
        )
    )

    return normalized
