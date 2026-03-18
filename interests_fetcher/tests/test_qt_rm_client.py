
import json
from pathlib import Path
import pytest
from interests_fetcher.qt_rm_client import QTRMClient

# ------------------ Fake HTTP layer ------------------

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or json.dumps(self._json)

    def json(self):
        return self._json


class FakeSession:
    """
    Минималистичный stateful-эмулятор requests.Session для тестов.
    Поддерживает .post/.get/.request и регистр маршрутов с обработчиками.
    """
    def __init__(self):
        self.handlers = {}  # (method, path) -> callable(headers, kwargs) -> FakeResponse
        self.requests = []  # список всех вызовов для ассертов

    def route(self, method, path, func):
        self.handlers[(method.upper(), path)] = func

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.requests.append((method.upper(), url, headers or {}, kwargs))
        method = method.upper()
        # Выделяем path из URL
        if "://" in url:
            after = url.split("://", 1)[1]
            path = "/" + after.split("/", 1)[1] if "/" in after else "/"
        else:
            path = url
        key = (method, path)
        handler = self.handlers.get(key)
        if not handler:
            return FakeResponse(404, {"error": f"no handler for {method} {path}"})
        return handler(headers or {}, kwargs)

    # удобные врапперы
    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


# ------------------ Tests ------------------

def test_health_triggers_login_and_uses_access_token(tmp_path):
    sess = FakeSession()

    issued = {"access": "A1", "refresh": "R1", "expires_in": 60}

    def login_handler(headers, kwargs):
        body = kwargs.get("json") or {}
        assert body.get("username") == "u"
        assert body.get("password") == "p"
        return FakeResponse(200, {
            "access_token": issued["access"],
            "refresh_token": issued["refresh"],
            "expires_in": issued["expires_in"],
        })

    def health_handler(headers, kwargs):
        assert headers.get("Authorization") == f"Bearer {issued['access']}"
        return FakeResponse(200, {"status": "ok"})

    sess.route("POST", "/auth/login", login_handler)
    sess.route("GET", "/health", health_handler)

    client = QTRMClient("http://api", "u", "p", session=sess)
    res = client.health()
    assert res["status"] == "ok"


def test_401_triggers_refresh_and_retry():
    sess = FakeSession()
    tokens = {"access": "A1", "refresh": "R1", "expires_in": 60}
    rotated = {"access": "A2", "refresh": "R2", "expires_in": 60}

    def login_handler(headers, kwargs):
        return FakeResponse(200, {
            "access_token": tokens["access"],
            "refresh_token": tokens["refresh"],
            "expires_in": tokens["expires_in"],
        })

    def refresh_handler(headers, kwargs):
        body = kwargs.get("json") or {}
        assert body.get("refresh_token") == tokens["refresh"]
        return FakeResponse(200, {
            "access_token": rotated["access"],
            "refresh_token": rotated["refresh"],
            "expires_in": rotated["expires_in"],
        })

    def list_models_handler(headers, kwargs):
        auth = headers.get("Authorization", "")
        if auth == f"Bearer {tokens['access']}":
            return FakeResponse(401, {"detail": "Access expired"})
        elif auth == f"Bearer {rotated['access']}":
            return FakeResponse(200, {"models": []})
        return FakeResponse(403, {"detail": "Forbidden"})

    sess.route("POST", "/auth/login", login_handler)
    sess.route("POST", "/auth/refresh", refresh_handler)
    sess.route("GET", "/model", list_models_handler)

    client = QTRMClient("http://api", "u", "p", session=sess)
    models = client.list_models()
    assert models["models"] == []
    assert any(m == "POST" and "/auth/refresh" in u for (m, u, _, _) in sess.requests)


def test_recognize_sends_multipart_and_passes_options(tmp_path):
    sess = FakeSession()
    tokens = {"access": "A1", "refresh": "R1", "expires_in": 60}

    def login_handler(headers, kwargs):
        return FakeResponse(200, {
            "access_token": tokens["access"],
            "refresh_token": tokens["refresh"],
            "expires_in": tokens["expires_in"],
        })

    def recognize_handler(headers, kwargs):
        assert headers.get("Authorization") == f"Bearer {tokens['access']}"
        files = kwargs.get("files") or {}
        assert "video_file" in files
        data = kwargs.get("data") or {}
        assert data.get("target_fps") == "10"
        assert data.get("no_normalize_labels") == "true"
        return FakeResponse(200, {"counts": {"free": 42}, "meta": {}, "events": [], "segments": []})

    sess.route("POST", "/auth/login", login_handler)
    sess.route("POST", "/tools/recognize", recognize_handler)

    vpath = tmp_path / "vid.mp4"
    vpath.write_bytes(b"0000")

    client = QTRMClient("http://api", "u", "p", session=sess)
    res = client.recognize(str(vpath), target_fps=10, no_normalize_labels=True)
    assert res["counts"]["free"] == 42


def test_tokens_persist_between_process_restarts(tmp_path):
    sess = FakeSession()
    first_issue = {"access": "A1", "refresh": "R1", "expires_in": 60}

    def login_handler(headers, kwargs):
        return FakeResponse(200, {
            "access_token": first_issue["access"],
            "refresh_token": first_issue["refresh"],
            "expires_in": first_issue["expires_in"],
        })

    def health_handler(headers, kwargs):
        assert headers.get("Authorization") == f"Bearer {first_issue['access']}"
        return FakeResponse(200, {"status": "ok"})

    sess.route("POST", "/auth/login", login_handler)
    sess.route("GET", "/health", health_handler)

    tokens_file = tmp_path / "tokens.json"

    c1 = QTRMClient("http://api", "u", "p", session=sess, tokens_path=tokens_file)
    assert c1.health()["status"] == "ok"
    assert tokens_file.exists()

    sess2 = FakeSession()
    sess2.route("GET", "/health", health_handler)
    c2 = QTRMClient("http://api", "u", "p", session=sess2, tokens_path=tokens_file)
    assert c2.health()["status"] == "ok"
    assert not any(m == "POST" and "/auth/login" in u for (m, u, _, _) in sess2.requests)


def test_recognize_webdav_urlencoded_body():
    sess = FakeSession()
    tokens = {"access": "A1", "refresh": "R1", "expires_in": 60}

    def login_handler(headers, kwargs):
        return FakeResponse(200, {
            "access_token": tokens["access"],
            "refresh_token": tokens["refresh"],
            "expires_in": tokens["expires_in"],
        })

    def recognize_wd_handler(headers, kwargs):
        assert headers.get("Authorization") == f"Bearer {tokens['access']}"
        data = kwargs.get("data") or {}
        assert data.get("interest_name") == "K630AX702_2025.10.14 09.34.33-09.37.19"
        assert data.get("target_fps") == "8"
        return FakeResponse(200, {"counts": {}, "meta": {"ok": True}, "events": [], "segments": []})

    sess.route("POST", "/auth/login", login_handler)
    sess.route("POST", "/tools/recognize_webdav", recognize_wd_handler)

    client = QTRMClient("http://api", "u", "p", session=sess)
    res = client.recognize_webdav("K630AX702_2025.10.14 09.34.33-09.37.19", target_fps=8)
    assert res["meta"]["ok"] is True
