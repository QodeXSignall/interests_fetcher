"""
Скачивает полные дни видео (по чанкам) для нескольких трегеров параллельно
через cms_gate: POST /download-clips-for-interest -> GET /video/file.

Поведение при offline-устройстве:
  - Перед каждым чанком проверяет статус через /devices.
  - Если offline — спит по расписанию BACKOFF (10 мин × 6, 30 мин × 4, 60 мин × 3).
  - Если за весь budget online не вернулся — трегер дропается, воркер берёт следующий
    из очереди (TRUCKS_QUEUE), пока есть backup-трегеры.
  - Если cms_gate отдаёт 500 на download-clips-for-interest — трактуем как маркер
    "device offline" и идём в backoff-петлю.

Файлы складываются в OUTPUT_DIR/<reg>/<YYYY-MM-DD>/ch<n>/<HHMMSS>-<HHMMSS>.mp4
Идемпотентно: уже скачанные чанки пропускаются.
"""

from __future__ import annotations

import os
import sys
import time
import json
import datetime as dt
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    pass


DATES = ["2026-05-13", "2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17"]

# (reg_id, channel, label, dates_override or None)
TRUCKS_QUEUE = [
    ("018270283681", 1, "К483РХ702", None),
    ("018270283642", 1, "К745ОН702", None),
    ("018270348452", 0, "K630AX702",
     ["2026-05-13", "2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17", "2026-05-18"]),
    ("108411", 1, "A939CA702", None),
    ("111352", 1, "Е028УУ702",
     ["2026-05-13", "2026-05-14", "2026-05-15", "2026-05-18", "2026-05-20"]),
]
WORKERS = 5
WINDOW = ("06:00:00", "22:00:00")
CHUNK_MINUTES = 30

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "interests_fetcher" / "output" / "videos"
PER_CHUNK_TIMEOUT_SEC = 300.0
DOWNLOAD_STREAM_TIMEOUT_SEC = 1800.0

# Сколько раз вернуться ко всем "отложенным" чанкам трегера прежде чем закончить
SECOND_PASS_ROUNDS = 3
SECOND_PASS_PAUSE_SEC = 600  # пауза между раундами 2nd pass

# backoff при offline (секунды между проверками /devices/{reg}/status)
BACKOFF_SCHEDULE_SEC = [600] * 6 + [1800] * 4 + [3600] * 3  # 1h + 2h + 3h = 6h budget

CMS_GATE_BASE_URL = os.environ.get("CMS_GATE_BASE_URL", "http://82.146.45.88:8081/api/v1")
CMS_GATE_API_TOKEN = os.environ.get("CMS_GATE_API_TOKEN", "")


def auth_headers() -> dict:
    if not CMS_GATE_API_TOKEN:
        return {}
    return {"Authorization": f"Bearer {CMS_GATE_API_TOKEN}"}


def iter_chunks(date: str, start: str, end: str, minutes: int):
    s = dt.datetime.strptime(f"{date} {start}", "%Y-%m-%d %H:%M:%S")
    e = dt.datetime.strptime(f"{date} {end}", "%Y-%m-%d %H:%M:%S")
    cur = s
    step = dt.timedelta(minutes=minutes)
    while cur < e:
        nxt = min(cur + step, e)
        yield cur, nxt
        cur = nxt


def chunk_dest(folder: str, ch: int, t0: dt.datetime, t1: dt.datetime) -> Path:
    day = t0.strftime("%Y-%m-%d")
    fname = f"{t0.strftime('%H%M%S')}-{t1.strftime('%H%M%S')}.mp4"
    return OUTPUT_DIR / folder / day / f"ch{ch}" / fname


def is_device_online(client: httpx.Client, reg_id: str) -> bool | None:
    """True если online, False если offline, None если ответ непонятен."""
    try:
        r = client.get(
            f"{CMS_GATE_BASE_URL}/devices",
            headers=auth_headers(),
            params={"status": "all"},
            timeout=30.0,
        )
        if r.status_code != 200:
            return None
        for d in (r.json() or {}).get("devices", []):
            if d.get("did") == reg_id:
                return (d.get("status") == "online") or (d.get("online") == 1)
        return None
    except Exception:
        return None


def call_download_clips(client: httpx.Client, reg_id: str, ch: int,
                        t0: dt.datetime, t1: dt.datetime) -> httpx.Response:
    name = f"{reg_id}_{t0.strftime('%Y.%m.%d %H.%M.%S')}-{t1.strftime('%H.%M.%S')}"
    interest = {
        "name": name,
        "reg_id": reg_id,
        "start_time": t0.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": t1.strftime("%Y-%m-%d %H:%M:%S"),
    }
    payload = {"reg_id": reg_id, "interest": interest, "channels": [ch]}
    return client.post(
        f"{CMS_GATE_BASE_URL}/download-clips-for-interest",
        json=payload,
        headers=auth_headers(),
        timeout=PER_CHUNK_TIMEOUT_SEC,
    )


def stream_file(client: httpx.Client, remote_path: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    bytes_written = 0
    with client.stream(
        "GET",
        f"{CMS_GATE_BASE_URL}/video/file",
        params={"path": remote_path},
        headers=auth_headers(),
        timeout=DOWNLOAD_STREAM_TIMEOUT_SEC,
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"video/file status={resp.status_code} body={resp.read()[:200]!r}")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
                bytes_written += len(chunk)
    os.replace(tmp, dest)
    return bytes_written


class TruckQueue:
    def __init__(self, items):
        self._dq = deque(items)
        self._lock = threading.Lock()

    def pop(self):
        with self._lock:
            return self._dq.popleft() if self._dq else None


def wait_for_online(client: httpx.Client, reg_id: str, label: str, emit) -> bool:
    """Возвращает True если устройство стало online в пределах budget."""
    for i, sleep_for in enumerate(BACKOFF_SCHEDULE_SEC, start=1):
        emit(f"[offline] {label} ({reg_id}) offline; ожидание #{i}/{len(BACKOFF_SCHEDULE_SEC)} {sleep_for//60} мин")
        time.sleep(sleep_for)
        online = is_device_online(client, reg_id)
        if online is True:
            emit(f"[online] {label} ({reg_id}) вернулся в online — продолжаем")
            return True
        if online is None:
            emit(f"[offline] {label} ({reg_id}) статус неопределён, считаем offline")
    emit(f"[give-up] {label} ({reg_id}) не вернулся за {sum(BACKOFF_SCHEDULE_SEC)//3600}h — дроп трегера")
    return False


def _attempt_chunk(
    client: httpx.Client,
    reg_id: str,
    channel: int,
    label: str,
    t0: dt.datetime,
    t1: dt.datetime,
    dest: Path,
    local: dict,
    emit,
) -> str:
    """Пытается скачать один чанк. Возвращает один из:
      'ok', 'empty', 'deferred' (отложен — кратковременная ошибка, повторим позже),
      'error_stream' (фатальная ошибка скачивания файла), 'dropped' (offline budget исчерпан).
    """
    tag = f"{reg_id} ch{channel} {t0.strftime('%Y-%m-%d %H:%M')}-{t1.strftime('%H:%M')}"
    t_start = time.time()

    try:
        resp = call_download_clips(client, reg_id, channel, t0, t1)
    except httpx.ReadTimeout:
        # cms_gate висел дольше PER_CHUNK_TIMEOUT_SEC — отложим
        online = is_device_online(client, reg_id)
        if online is False:
            if not wait_for_online(client, reg_id, label, emit):
                return "dropped"
            return "deferred"
        emit(f"[timeout] {tag}: cms_gate hang > {int(PER_CHUNK_TIMEOUT_SEC)}s; отложен")
        return "deferred"
    except Exception as e:
        emit(f"[ERR clips] {tag}: {type(e).__name__}: {e}; отложен")
        return "deferred"

    # Структурированные коды от cms_gate
    if resp.status_code in (502, 503):
        body = {}
        try:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            body = {}
        kind = body.get("kind") or ""
        detail = body.get("detail") or (resp.text or "")[:200]
        if resp.status_code == 503 or kind == "device_offline":
            if not wait_for_online(client, reg_id, label, emit):
                return "dropped"
            return "deferred"
        # 502: cms_retry_exhausted (глушение) / cms_error — отложим
        emit(f"[{resp.status_code} {kind or 'cms_error'}] {tag}: {detail!r}; отложен")
        return "deferred"

    if resp.status_code == 500:
        # legacy/неструктурированный — трактуем как deferred
        emit(f"[500] {tag}: {(resp.text or '')[:200]!r}; отложен")
        return "deferred"

    if resp.status_code != 200:
        emit(f"[ERR clips] {tag}: HTTP {resp.status_code} {(resp.text or '')[:200]!r}; отложен")
        return "deferred"

    data = resp.json()
    info = (data.get("channels") or {}).get(str(channel)) or \
           (data.get("channels") or {}).get(channel) or {}
    remote_path = info.get("path") if isinstance(info, dict) else None
    if not remote_path:
        emit(f"[empty] {tag}: cms_gate path=None elapsed={time.time()-t_start:.1f}s")
        return "empty"

    try:
        written = stream_file(client, remote_path, dest)
    except Exception as e:
        emit(f"[ERR stream] {tag} path={remote_path}: {type(e).__name__}: {e}")
        return "error_stream"

    local["ok"] += 1
    local["bytes"] += written
    emit(
        f"[ok]   {tag} -> {dest.relative_to(OUTPUT_DIR)} "
        f"({written/1e6:.1f} MB, {time.time()-t_start:.1f}s)"
    )
    return "ok"


def process_truck(reg_id: str, channel: int, label: str, emit, dates: list[str] | None = None) -> dict:
    """Возвращает локальную сводку. Если трегер дропнут — содержит 'dropped': True."""
    local = {"truck": label, "reg_id": reg_id, "ok": 0, "skipped": 0, "empty": 0,
             "errors": 0, "deferred_final": 0, "bytes": 0, "dropped": False}
    dates_to_use = dates if dates else DATES
    emit(f"[start] worker -> {label} ({reg_id} ch{channel}) dates={dates_to_use}")

    # Собираем план: все чанки трегера
    all_chunks: list[tuple[dt.datetime, dt.datetime]] = []
    for date in dates_to_use:
        for t0, t1 in iter_chunks(date, WINDOW[0], WINDOW[1], CHUNK_MINUTES):
            all_chunks.append((t0, t1))

    with httpx.Client(http2=False) as client:
        # быстрая проверка online перед стартом
        online = is_device_online(client, reg_id)
        if online is False:
            if not wait_for_online(client, reg_id, label, emit):
                local["dropped"] = True
                return local

        # Round 0: первичный проход. Чанки, которые не получилось — в pending.
        # Round 1..N: ретраи pending с паузой между раундами.
        pending: list[tuple[dt.datetime, dt.datetime]] = list(all_chunks)
        max_round = SECOND_PASS_ROUNDS  # 0..N
        for rnd in range(0, max_round + 1):
            if not pending:
                break
            if rnd > 0:
                emit(f"[pass {rnd}/{max_round}] {label}: {len(pending)} отложенных чанков; пауза {SECOND_PASS_PAUSE_SEC//60} мин")
                time.sleep(SECOND_PASS_PAUSE_SEC)
                emit(f"[pass {rnd}/{max_round}] {label}: старт повторного прохода")

            next_pending: list[tuple[dt.datetime, dt.datetime]] = []
            for t0, t1 in pending:
                dest = chunk_dest(label, channel, t0, t1)
                if dest.exists() and dest.stat().st_size > 0:
                    if rnd == 0:
                        local["skipped"] += 1
                        emit(f"[skip] {dest.relative_to(OUTPUT_DIR)} ({dest.stat().st_size} B)")
                    continue

                outcome = _attempt_chunk(client, reg_id, channel, label, t0, t1, dest, local, emit)
                if outcome == "ok":
                    pass
                elif outcome == "empty":
                    local["empty"] += 1
                elif outcome == "error_stream":
                    local["errors"] += 1
                elif outcome == "dropped":
                    local["dropped"] = True
                    local["deferred_final"] += len(next_pending) + 1
                    return local
                else:  # deferred
                    next_pending.append((t0, t1))

            pending = next_pending

        if pending:
            local["deferred_final"] = len(pending)
            emit(f"[deferred_final] {label}: {len(pending)} чанков остались неудачными после {max_round} повторных проходов")
            for t0, t1 in pending:
                emit(f"  unfetched: {reg_id} ch{channel} {t0.strftime('%Y-%m-%d %H:%M')}-{t1.strftime('%H:%M')}")

    return local


def main() -> int:
    if not CMS_GATE_API_TOKEN:
        print("ERROR: CMS_GATE_API_TOKEN не задан (env или .env)", flush=True)
        return 2
    print(f"cms_gate: {CMS_GATE_BASE_URL}", flush=True)
    print(f"trucks_queue: {TRUCKS_QUEUE}", flush=True)
    print(f"workers: {WORKERS}  window={WINDOW} chunk={CHUNK_MINUTES}m", flush=True)
    print(f"dates: {DATES}", flush=True)
    print(f"backoff: {BACKOFF_SCHEDULE_SEC} sec (sum={sum(BACKOFF_SCHEDULE_SEC)//60} min)", flush=True)
    print(f"output: {OUTPUT_DIR}", flush=True)

    queue = TruckQueue(TRUCKS_QUEUE)
    summary = {"trucks_done": 0, "trucks_dropped": 0,
               "ok": 0, "skipped": 0, "empty": 0, "errors": 0, "bytes": 0}
    print_lock = threading.Lock()
    sum_lock = threading.Lock()

    def emit(line: str) -> None:
        with print_lock:
            print(line, flush=True)

    def worker_loop(worker_id: int) -> None:
        while True:
            item = queue.pop()
            if not item:
                emit(f"[worker {worker_id}] queue empty -> exit")
                return
            reg_id, channel, label, dates_override = item[0], item[1], item[2], (item[3] if len(item) > 3 else None)
            local = process_truck(reg_id, channel, label, emit, dates_override)
            with sum_lock:
                summary["ok"] += local["ok"]
                summary["skipped"] += local["skipped"]
                summary["empty"] += local["empty"]
                summary["errors"] += local["errors"]
                summary["bytes"] += local["bytes"]
                if local["dropped"]:
                    summary["trucks_dropped"] += 1
                else:
                    summary["trucks_done"] += 1
            emit(f"[done] worker {worker_id}: {label} -> {local}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(worker_loop, i + 1) for i in range(WORKERS)]
        for f in futures:
            f.result()

    print("---", flush=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
