
from __future__ import annotations
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

import httpx


class QTRMAuthError(Exception):
    pass


class QTRMClientError(Exception):
    pass


class QTRMAsyncClient:
    """
    Асинхронный клиент для QTracker Post-Processor API на базе httpx.AsyncClient.
    - Сам логинится и ротирует refresh токен.
    - Прозрачно обновляет access при 401 (одна попытка).
    - Опционально сохраняет токены на диск (tokens_path).
    - Потокобезопасность: asyncio.Lock вокруг refresh/login.
    - Контекст-менеджер: `async with QTRMAsyncClient(...) as cli:`

    Параметры:
        base_url     : URL сервиса (например, http://127.0.0.1:1337)
        username     : логин
        password     : пароль
        tokens_path  : путь до JSON-файла для персистентности токенов (опционально)
        timeout      : таймаут запросов в секундах (float)
        client       : внешний httpx.AsyncClient (опционально)
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        tokens_path: Optional[Union[str, Path]] = None,
        timeout: float = 600.0,
        client: Optional[httpx.AsyncClient] = None,
        concurrent_requests: int = 16,
    ) -> None:
        if not base_url or not username or not password:
            raise ValueError("base_url, username, password are required")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

        self.timeout = timeout
        self._client = client  # может быть None, создадим на __aenter__ при необходимости

        # Токены в памяти
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._access_expires_at: Optional[float] = None  # epoch seconds

        # Файл с токенами (персистентность)
        self.tokens_path = Path(tokens_path) if tokens_path else None
        if self.tokens_path:
            self._load_tokens_file()

        # Лимит параллельных запросов к QTRM
        self._sem = asyncio.Semaphore(concurrent_requests)

        # Блокировка на refresh/login
        self._lock = asyncio.Lock()

    # ---------------- context management ----------------
    async def __aenter__(self) -> "QTRMAsyncClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------- utils ----------------
    def _now(self) -> float:
        return time.time()

    # ---------------- tokens persistence ----------------
    def _load_tokens_file(self) -> None:
        try:
            data = json.loads(self.tokens_path.read_text(encoding="utf-8"))
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._access_expires_at = data.get("access_expires_at")
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[QTRMAsync] warn: failed to load tokens file: {e}")

    def _save_tokens_file(self) -> None:
        if not self.tokens_path:
            return
        payload = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "access_expires_at": self._access_expires_at,
        }
        self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
        self.tokens_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(self.tokens_path, 0o600)
        except Exception:
            pass

    # ---------------- auth flow ----------------
    async def _login(self) -> None:
        assert self._client is not None, "Client not started. Use 'async with' or call __aenter__()."
        url = f"{self.base_url}/auth/login"
        resp = await self._client.post(url, json={"username": self.username, "password": self.password})
        if resp.status_code != 200:
            raise QTRMAuthError(f"login failed: {resp.status_code} {resp.text}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._access_expires_at = self._now() + int(data["expires_in"])
        self._save_tokens_file()

    async def _refresh(self) -> None:
        assert self._client is not None, "Client not started. Use 'async with' or call __aenter__()."
        if not self._refresh_token:
            await self._login()
            return

        url = f"{self.base_url}/auth/refresh"
        resp = await self._client.post(url, json={"refresh_token": self._refresh_token})
        if resp.status_code != 200:
            # refresh не удался — перезаходим
            await self._login()
            return

        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._access_expires_at = self._now() + int(data["expires_in"])
        self._save_tokens_file()

    async def _ensure_access(self) -> None:
        """Гарантирует актуальный access_token (login/refresh если нужно)."""
        async with self._lock:
            if not self._access_token or not self._access_expires_at:
                await self._login()
                return
            if self._now() >= float(self._access_expires_at) - 10:
                await self._refresh()

    # ---------------- low-level request ----------------
    async def _request(self, method: str, path: str, *, retry_on_401: bool = True, **kwargs) -> httpx.Response:
        """
        Делает запрос с Bearer-авторизацией.
        Если получаем 401 — делаем один refresh и повторяем.
        """
        if self._client is None:
            # позволяем использовать без контекст-менеджера, но создадим клиента
            self._client = httpx.AsyncClient(timeout=self.timeout)

        await self._ensure_access()
        headers = kwargs.pop("headers", {}) or {}
        headers["Authorization"] = f"Bearer {self._access_token}"
        url = f"{self.base_url}{path}"

        resp = await self._client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 401 and retry_on_401:
            async with self._lock:
                await self._refresh()
                headers["Authorization"] = f"Bearer {self._access_token}"
                resp = await self._client.request(method, url, headers=headers, **kwargs)
        return resp

    # ---------------- public API ----------------
    # ---- system
    async def health(self) -> Dict[str, Any]:
        # health открыт — можно без токена, но мы не усложняем и всегда идём через _request
        resp = await self._request("GET", "/health")
        if resp.status_code != 200:
            raise QTRMClientError(f"health failed: {resp.status_code} {resp.text}")
        return resp.json()

    # ---- model
    async def list_models(self) -> Dict[str, Any]:
        resp = await self._request("GET", "/model")
        if resp.status_code != 200:
            raise QTRMClientError(f"list_models failed: {resp.status_code} {resp.text}")
        return resp.json()

    async def upload_model(
        self,
        weights_path: Union[str, Path],
        *,
        labels_path: Optional[Union[str, Path]] = None,
        device: Optional[str] = None,
    ) -> Dict[str, Any]:
        files = {}
        # httpx принимает файловые объекты (sync) — это ок для большинства случаев
        wp = Path(weights_path)
        files["model_file"] = ("weights.pt", wp.open("rb"))
        data: Dict[str, Any] = {}
        if device:
            data["device"] = device
        if labels_path:
            lp = Path(labels_path)
            files["labels_file"] = ("labels.txt", lp.open("rb"))

        try:
            resp = await self._request("POST", "/model", files=files, data=data)
        finally:
            try:
                files["model_file"][1].close()
            except Exception:
                pass
            if "labels_file" in files:
                try:
                    files["labels_file"][1].close()
                except Exception:
                    pass

        if resp.status_code != 200:
            raise QTRMClientError(f"upload_model failed: {resp.status_code} {resp.text}")
        return resp.json()

    async def delete_model(self, model_id: str) -> Dict[str, Any]:
        resp = await self._request("DELETE", f"/model/{model_id}")
        if resp.status_code != 200:
            raise QTRMClientError(f"delete_model failed: {resp.status_code} {resp.text}")
        return resp.json()

    async def set_current_model(self, model_id: str) -> Dict[str, Any]:
        resp = await self._request("POST", f"/model/{model_id}/set_current")
        if resp.status_code != 200:
            raise QTRMClientError(f"set_current_model failed: {resp.status_code} {resp.text}")
        return resp.json()

    async def recognize(
            self,
            video_path: Union[str, Path],
            *,
            model_id: Optional[str] = None,
            device: Optional[str] = None,
            target_fps: Optional[float] = None,
            stride: Optional[int] = None,
            imgsz: Optional[int] = None,
            batch: Optional[int] = None,
            smooth_window: Optional[int] = None,
            min_sec_by_label: Optional[str] = None,
            max_noise_gap_sec: Optional[float] = None,
            free_labels: Optional[str] = None,
            no_normalize_labels: bool = False,
            finalize_free_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Загружает локальный видеофайл на QTRM /tools/recognize.
        Обёрнуто в семафор self._sem — ограничение параллельности.
        """
        async with self._sem:
            files: Dict[str, Any] = {}
            vp = Path(video_path)
            files["video_file"] = (vp.name or "video.mp4", vp.open("rb"))

            data: Dict[str, Any] = {}
            if model_id is not None:
                data["model_id"] = model_id
            if device is not None:
                data["device"] = device
            if target_fps is not None:
                data["target_fps"] = str(target_fps)
            if stride is not None:
                data["stride"] = str(stride)
            if imgsz is not None:
                data["imgsz"] = str(imgsz)
            if batch is not None:
                data["batch"] = str(batch)
            if smooth_window is not None:
                data["smooth_window"] = str(smooth_window)
            if min_sec_by_label is not None:
                data["min_sec_by_label"] = min_sec_by_label
            if max_noise_gap_sec is not None:
                data["max_noise_gap_sec"] = str(max_noise_gap_sec)
            if free_labels is not None:
                data["free_labels"] = free_labels
            if no_normalize_labels:
                data["no_normalize_labels"] = "true"
            if finalize_free_sec is not None:
                data["finalize_free_sec"] = str(finalize_free_sec)

            try:
                resp = await self._request("POST", "/tools/recognize", files=files, data=data)
            finally:
                try:
                    files["video_file"][1].close()
                except Exception:
                    pass

            if resp.status_code != 200:
                raise QTRMClientError(f"recognize failed: {resp.status_code} {resp.text}")
            return resp.json()

    async def recognize_webdav(
            self,
            interest_name: str,
            *,
            model_id: Optional[str] = None,
            device: Optional[str] = None,
            target_fps: Optional[float] = None,
            stride: Optional[int] = None,
            imgsz: Optional[int] = None,
            batch: Optional[int] = None,
            smooth_window: Optional[int] = None,
            min_sec_by_label: Optional[str] = None,
            max_noise_gap_sec: Optional[float] = None,
            free_labels: Optional[str] = None,
            no_normalize_labels: bool = False,
            finalize_free_sec: Optional[float] = None,
            webdav_root: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Стартует задачу на QTRM /tasks/recognize_webdav_task по имени интереса.
        Обёрнуто в семафор self._sem — ограничение параллельности.
        """
        async with self._sem:
            params: Dict[str, Any] = {"interest_name": interest_name}
            if model_id is not None:
                params["model_id"] = model_id
            if device is not None:
                params["device"] = device
            if target_fps is not None:
                params["target_fps"] = str(target_fps)
            if stride is not None:
                params["stride"] = str(stride)
            if imgsz is not None:
                params["imgsz"] = str(imgsz)
            if batch is not None:
                params["batch"] = str(batch)
            if smooth_window is not None:
                params["smooth_window"] = str(smooth_window)
            if min_sec_by_label is not None:
                params["min_sec_by_label"] = min_sec_by_label
            if max_noise_gap_sec is not None:
                params["max_noise_gap_sec"] = str(max_noise_gap_sec)
            if free_labels is not None:
                params["free_labels"] = free_labels
            if no_normalize_labels:
                params["no_normalize_labels"] = "true"
            if finalize_free_sec is not None:
                params["finalize_free_sec"] = str(finalize_free_sec)
            if webdav_root is not None:
                params["webdav_root"] = webdav_root

            # Основной вариант — query-параметры
            resp = await self._request("POST", "/tasks/recognize_webdav_task", params=params)
            if resp.status_code == 200:
                return resp.json()

            # Fallback на form-data, если сервер так ожидает (422)
            if resp.status_code == 422:
                resp2 = await self._request("POST", "/tasks/recognize_webdav_task", data=params)
                if resp2.status_code == 200:
                    return resp2.json()

            raise QTRMClientError(f"recognize_webdav failed: {resp.status_code} {resp.text}")

    # ---- helpers
    async def force_login(self) -> None:
        async with self._lock:
            await self._login()

    async def get_access_token(self) -> str:
        await self._ensure_access()
        assert self._access_token
        return self._access_token

import asyncio
from interests_fetcher.data import settings

async def main():
    async with QTRMAsyncClient(
        base_url=settings.qt_rm_url,
        username=settings.qt_rm_login,
        password=settings.qt_rm_password,
    ) as cli:
        print(await cli.health())
        res = await cli.recognize_webdav("K630AX702_2025.10.24 11.52.13-11.55.21")
        print(res)

#asyncio.run(main())