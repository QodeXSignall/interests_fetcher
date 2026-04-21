import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from interests_fetcher.interest_merge_funcs import merge_overlapping_interests
from interests_fetcher import functions as main_funcs
from interests_fetcher import cms_gate_client
from main_operator import Main
import asyncio
import httpx
import logging
import re
from datetime import datetime
from pathlib import Path


#K630AX702_2025.10.30 10.05.08-10.16.49
#K630AX702_2025.12.01 08.51.15-08.53.20
#10:05:07
# 10:16:42
# Можно передавать либо DevIDNO регистратора (например "018270283642"),
# либо госномер машины (например "К745ОН702") — второй вариант будет
# отрезолвлен в DevIDNO через cms_gate (GET /trucks/by-plate/...).
DEVICE_OR_PLATE = "K745OH702"
START_TIME = "2026-04-18 06:00:00"
END_TIME = "2026-04-18 18:00:00"

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


OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def _build_output_path(device_or_plate: str, start_time: str, end_time: str) -> Path:
    def _slug(s: str) -> str:
        return re.sub(r"[^0-9A-Za-zА-Яа-я_-]+", "_", s).strip("_")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"interests_{_slug(device_or_plate)}_{_slug(start_time)}__{_slug(end_time)}_{stamp}.txt"
    return OUTPUT_DIR / name


class _Tee:
    """Duplicate writes to multiple text streams (e.g. original stdout + file)."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        for s in self._streams:
            try:
                if s.isatty():
                    return True
            except Exception:
                pass
        return False

    def fileno(self):
        raise OSError("Tee has no fileno")


def _install_output_capture(output_path: Path):
    """Mirror stdout/stderr and logging output to `output_path` while keeping
    the original console output. Returns a callable that restores state."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = output_path.open("w", encoding="utf-8", buffering=1)

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    sys.stdout = _Tee(orig_stdout, log_file)
    sys.stderr = _Tee(orig_stderr, log_file)

    root_logger = logging.getLogger()
    file_handler = logging.FileHandler(output_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    prev_level = root_logger.level
    if prev_level == logging.NOTSET or prev_level > logging.DEBUG:
        root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)

    def _restore():
        try:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
        finally:
            root_logger.removeHandler(file_handler)
            root_logger.setLevel(prev_level)
            try:
                file_handler.close()
            finally:
                log_file.close()

    return _restore


async def _resolve_reg_id(value: str) -> str:
    reg_id = await cms_gate_client.resolve_device_id(value)
    if not reg_id:
        raise SystemExit(
            f"Не удалось найти DevIDNO для {value!r}. "
            f"Убедитесь, что ТС заведена в cms_gate vehicle manager "
            f"(POST /api/v1/trucks) с корректным plate и mdvr.id."
        )
    if reg_id != value:
        print(f"[resolve] plate={value!r} -> DevIDNO={reg_id!r}")
    return reg_id


async def local_get_interests_async():
    try:
        print(f"device_or_plate: {DEVICE_OR_PLATE}")
        print(f"start_time: {START_TIME}")
        print(f"end_time: {END_TIME}")

        await inst.login()

        reg_id = await _resolve_reg_id(DEVICE_OR_PLATE)
        print(f"reg_id: {reg_id}")
        reg_info = main_funcs.get_reg_info(reg_id=reg_id)

        interests = await inst.get_interests_async(
            reg_id=reg_id,
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

        # --- Этап 2/2.5: прогнать reconcile/fallback по due-гэпам ---
        # `misc/get_interests.py` не ходит через `_refill_pending_interests_if_due`,
        # поэтому явно дёргаем gap-проход, чтобы в выводе был виден эффект recheck
        # (last_checked, no_progress_streak, closed/abandoned, fallback-интересы).
        print("\n=== GAP RECHECK ===")
        try:
            await inst._process_gaps_if_due(reg_id)
        except Exception as e:
            print(f"[gap_recheck] Ошибка: {e!r}")

        # --- gaps (Этап 5): текущее состояние разрывов в states для регистратора ---
        try:
            gaps = main_funcs.get_gaps(reg_id)
        except Exception as e:
            print(f"\n[gaps] Не удалось прочитать gaps: {e}")
            gaps = []

        if gaps:
            by_status: dict[str, list[dict]] = {}
            for g in gaps:
                by_status.setdefault(g.get("status", "?"), []).append(g)
            print("\n=== GAPS ===")
            print(
                f"Всего: {len(gaps)} "
                f"(pending={len(by_status.get('pending', []))}, "
                f"closed={len(by_status.get('closed', []))}, "
                f"abandoned={len(by_status.get('abandoned', []))})"
            )
            for status in ("pending", "closed", "abandoned"):
                lst = by_status.get(status) or []
                if not lst:
                    continue
                lst_sorted = sorted(lst, key=lambda g: g.get("gap_start", ""))
                print(f"\n-- {status.upper()} ({len(lst_sorted)}) --")
                for g in lst_sorted:
                    linked = g.get("linked_interest_names") or []
                    print(
                        f"  id={g.get('id')} "
                        f"[{g.get('gap_start')} → {g.get('gap_end')}] "
                        f"created={g.get('created_at')} "
                        f"checked={g.get('checked')} "
                        f"last_checked={g.get('last_checked')} "
                        f"points_last_seen={g.get('points_last_seen')} "
                        f"no_progress_streak={g.get('no_progress_streak', 0)}"
                        + (f" closed_at={g.get('closed_at')}" if status != "pending" else "")
                        + (f" linked_interests={linked}" if linked else "")
                    )
        else:
            print("\n=== GAPS ===\nНет зарегистрированных разрывов.")

        # --- pending_interests: показываем какие интересы сейчас заблокированы gap'ами ---
        try:
            pending_now = main_funcs.get_pending_interests(reg_id)
        except Exception as e:
            print(f"\n[pending] Не удалось прочитать pending_interests: {e}")
            pending_now = []

        if pending_now:
            blocked = [it for it in pending_now if isinstance(it, dict) and it.get("blocking_gap_ids")]
            unblocked = [it for it in pending_now if isinstance(it, dict) and not it.get("blocking_gap_ids")]
            print(
                f"\n=== PENDING_INTERESTS ({len(pending_now)}) ===\n"
                f"  к выгрузке: {len(unblocked)}\n"
                f"  заблокировано gap'ами: {len(blocked)}"
            )
            for it in blocked:
                print(
                    f"  [BLOCKED] name={it.get('name')} "
                    f"[{it.get('start_time')} → {it.get('end_time')}] "
                    f"by_gap_ids={it.get('blocking_gap_ids')}"
                )
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
    output_path = _build_output_path(DEVICE_OR_PLATE, START_TIME, END_TIME)
    restore = _install_output_capture(output_path)
    try:
        asyncio.run(local_get_interests_async())
    finally:
        print(f"\n[saved] {output_path}")
        restore()
