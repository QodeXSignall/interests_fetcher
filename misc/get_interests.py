import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from interests_fetcher.interest_merge_funcs import merge_overlapping_interests
from interests_fetcher import functions as main_funcs
from interests_fetcher import cms_gate_client
from main_operator import Main
import asyncio
import httpx


#K630AX702_2025.10.30 10.05.08-10.16.49
#K630AX702_2025.12.01 08.51.15-08.53.20
#10:05:07
# 10:16:42
#REG_ID = "108411"
REG_ID = "К745ОН702"
START_TIME = "2026-03-20 06:00:00"
END_TIME = "2026-03-26 18:00:00"

# cms_gate auth is: `Authorization: Bearer <SERVICE_TOKEN>`
# (see `docs/cms_gate_rest_api.md`)
#
# Prefer env var `CMS_GATE_API_TOKEN`.
# If it is missing, fallback to the token value you provided.


def _ensure_cms_gate_auth() -> None:
    token = (os.environ.get("CMS_GATE_API_TOKEN") or "").strip()
    if token:
        return

    # Allow optional file-based secret injection:
    # export CMS_GATE_API_TOKEN_FILE=/path/to/token.txt
    token_file = (os.environ.get("CMS_GATE_API_TOKEN_FILE") or "").strip()
    if token_file:
        try:
            token = open(token_file, "r", encoding="utf-8").read().strip()
        except Exception:
            token = ""

    os.environ["CMS_GATE_API_TOKEN"] = token


_ensure_cms_gate_auth()


inst = Main()
reg_info = main_funcs.get_reg_info(reg_id=REG_ID)


async def local_get_interests_async():
    try:
        await inst.login()

        interests = await inst.get_interests_async(
            reg_id=REG_ID,
            reg_info=reg_info,
            start_time=START_TIME,
            stop_time=END_TIME,
        )

        if isinstance(interests, dict):
            print("\n=== РЕЗУЛЬТАТ ===")
            print(f"Сервис вернул ошибку: {interests.get('error', interests)}")
            return

        interests = merge_overlapping_interests(interests)
        print("\n=== РЕЗУЛЬТАТ ===")
        print(f"Найдено интересов: {len(interests)}")
        for interest in interests:
            print(interest)
    except httpx.ConnectError:
        print(
            "\nНе удалось подключиться к cms_gate. "
            "Проверь, что сервис запущен и доступен по CMS_GATE_BASE_URL "
            "(в `cms_gate_client` значение по умолчанию может отличаться; обычно это "
            "`http://localhost:<PORT>/api/v1`)."
        )
    except httpx.HTTPStatusError as e:
        # e.response may contain body with details (e.g., 401/403 for auth)
        status = getattr(e.response, "status_code", "unknown")
        body = (getattr(e.response, "text", "") or "")[:800]
        print(
            f"\ncms_gate вернул HTTP ошибку: status={status}\n"
            f"body (truncated): {body}"
        )
    finally:
        await cms_gate_client.close_client()


if __name__ == "__main__":
    asyncio.run(local_get_interests_async())
