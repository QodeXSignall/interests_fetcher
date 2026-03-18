import httpx

_client_async: httpx.AsyncClient | None = None


def get_cms_async_client() -> httpx.AsyncClient:
    global _client_async
    if _client_async is None:
        _client_async = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            timeout=httpx.Timeout(360.0, connect=5.0),
            headers={"User-Agent": "interests_fetcher/1.0"},
            http2=True,  # если сервер не поддерживает — можно поставить False
        )
    return _client_async


async def close_cms_async_client():
    global _client_async
    if _client_async is not None:
        await _client_async.aclose()
        _client_async = None