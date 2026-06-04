"""
Дамп треков и алармов из cms_gate (GET /tracks-alarms) на локальный диск.

Цель — заморозить сырые данные за интересующие нам дни, чтобы на сервере они
случайно не перезаписались, пока человек смотрит видео. Когда отчёт готов,
по этим JSON-ам можно гонять любые варианты алгоритма поиска интересов.

Складывается в:
    interests_fetcher/new_alg/raw_data/<plate>/<YYYY-MM-DD>.json
Каждый файл — это {"reg_id","plate","date","window","tracks","alarms","fetched_at",
                   "chunk_starts": [...]}.

Идемпотентно: если файл уже есть и содержит непустые tracks/alarms (или явный
маркер "empty"), повторно не качаем.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx


# Грузим .env (CMS_GATE_BASE_URL, CMS_GATE_API_TOKEN)
_ROOT = Path(__file__).resolve().parent.parent
for _line in (_ROOT / ".env").read_text().splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


CMS_GATE_BASE_URL = os.environ["CMS_GATE_BASE_URL"].rstrip("/")
CMS_GATE_API_TOKEN = os.environ.get("CMS_GATE_API_TOKEN", "")
AUTH_HEADERS = {"Authorization": f"Bearer {CMS_GATE_API_TOKEN}"} if CMS_GATE_API_TOKEN else {}

OUTPUT_DIR = _ROOT / "interests_fetcher" / "new_alg" / "raw_data"

# (reg_id, plate, [dates]). К180КЕ702 пропущена по просьбе пользователя.
JOBS: list[tuple[str, str, list[str]]] = [
    ("108411", "A939CA702", ["2026-05-13"]),
    ("018270348452", "K630AX702",
     ["2026-05-13", "2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17", "2026-05-18"]),
    ("111352", "Е028УУ702",
     ["2026-05-13", "2026-05-14", "2026-05-15", "2026-05-18", "2026-05-20"]),
    ("018270283681", "К483РХ702",
     ["2026-05-13", "2026-05-14", "2026-05-15", "2026-05-16"]),
    ("018270283642", "К745ОН702",
     ["2026-05-13", "2026-05-14"]),
]

CHUNK_HOURS = 2          # окно одного запроса tracks-alarms
TIMEOUT_SEC = 300.0       # таймаут на чанк
RETRY_COUNT = 3           # ретраи на 5xx / транспортные ошибки
RETRY_DELAY_SEC = 6.0


def _fmt_ts(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _track_key(t: dict) -> tuple:
    # Уникальный ключ записи трека: GPS-время + координаты + lc/lng/lat.
    return (t.get("gt"), t.get("lng"), t.get("lat"), t.get("lc"), t.get("sn"))


def _alarm_key(a: dict) -> tuple:
    # У CMS аларма стабильно уникален guid; recId/stm/etm — резерв на случай null.
    return (a.get("guid"), a.get("recId"), a.get("stm"), a.get("etm"), a.get("atp"))


def fetch_chunk(client: httpx.Client, reg_id: str, t0: dt.datetime, t1: dt.datetime
                ) -> tuple[list[dict], list[dict]]:
    last_err: Exception | None = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            r = client.get(
                f"{CMS_GATE_BASE_URL}/tracks-alarms",
                params={
                    "reg_id": reg_id,
                    "start_time": _fmt_ts(t0),
                    "end_time": _fmt_ts(t1),
                },
                headers=AUTH_HEADERS,
                timeout=TIMEOUT_SEC,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = RuntimeError(f"HTTP {r.status_code} body={(r.text or '')[:300]!r}")
                raise last_err
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} body={(r.text or '')[:600]!r}")
            data = r.json()
            return data.get("tracks") or [], data.get("alarms") or []
        except Exception as e:
            last_err = e
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY_SEC * attempt)
                continue
            raise


def dump_day(client: httpx.Client, reg_id: str, plate: str, date_str: str) -> dict[str, Any]:
    out_path = OUTPUT_DIR / plate / f"{date_str}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
            if existing.get("complete"):
                return {"plate": plate, "date": date_str, "skipped": True,
                        "tracks": len(existing.get("tracks") or []),
                        "alarms": len(existing.get("alarms") or [])}
        except Exception:
            pass  # перезапишем

    day = dt.datetime.strptime(date_str, "%Y-%m-%d")
    day_end = day + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    cur = day
    step = dt.timedelta(hours=CHUNK_HOURS)

    all_tracks: list[dict] = []
    all_alarms: list[dict] = []
    seen_tracks: set[tuple] = set()
    seen_alarms: set[tuple] = set()
    chunk_starts: list[str] = []
    errors: list[str] = []

    while cur <= day_end:
        nxt = min(cur + step - dt.timedelta(seconds=1), day_end)
        try:
            tracks, alarms = fetch_chunk(client, reg_id, cur, nxt)
            for t in tracks:
                k = _track_key(t)
                if k not in seen_tracks:
                    seen_tracks.add(k)
                    all_tracks.append(t)
            for a in alarms:
                k = _alarm_key(a)
                if k not in seen_alarms:
                    seen_alarms.add(k)
                    all_alarms.append(a)
            chunk_starts.append(_fmt_ts(cur))
            print(f"  [{plate} {date_str}] {_fmt_ts(cur)}..{_fmt_ts(nxt)} "
                  f"+{len(tracks)} tracks / +{len(alarms)} alarms "
                  f"(cum: {len(all_tracks)}/{len(all_alarms)})", flush=True)
        except Exception as e:
            err = f"{_fmt_ts(cur)}..{_fmt_ts(nxt)}: {type(e).__name__}: {e}"
            errors.append(err)
            print(f"  [{plate} {date_str}] FAIL {err}", flush=True)
        cur = cur + step

    payload = {
        "reg_id": reg_id,
        "plate": plate,
        "date": date_str,
        "window": [_fmt_ts(day), _fmt_ts(day_end)],
        "chunk_hours": CHUNK_HOURS,
        "chunk_starts": chunk_starts,
        "fetched_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tracks": all_tracks,
        "alarms": all_alarms,
        "errors": errors,
        "complete": not errors,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False))
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"  -> {out_path.relative_to(_ROOT)} "
          f"tracks={len(all_tracks)} alarms={len(all_alarms)} "
          f"size={size_mb:.1f}MB errors={len(errors)}", flush=True)
    return {"plate": plate, "date": date_str, "skipped": False,
            "tracks": len(all_tracks), "alarms": len(all_alarms),
            "errors": len(errors), "size_mb": round(size_mb, 1)}


def main() -> int:
    if not CMS_GATE_API_TOKEN:
        print("ERROR: CMS_GATE_API_TOKEN не задан", flush=True)
        return 2
    print(f"cms_gate: {CMS_GATE_BASE_URL}", flush=True)
    print(f"out:      {OUTPUT_DIR}", flush=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    summary: list[dict] = []
    with httpx.Client(http2=False) as client:
        for reg_id, plate, dates in JOBS:
            for date_str in dates:
                print(f"[{plate} {reg_id}] -> {date_str}", flush=True)
                try:
                    summary.append(dump_day(client, reg_id, plate, date_str))
                except Exception as e:
                    print(f"[{plate} {date_str}] FATAL: {type(e).__name__}: {e}", flush=True)
                    summary.append({"plate": plate, "date": date_str,
                                    "error": f"{type(e).__name__}: {e}"})

    print("---", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    (OUTPUT_DIR / "_summary.json").write_text(
        json.dumps({"generated_at": dt.datetime.utcnow().isoformat() + "Z",
                    "jobs": summary}, ensure_ascii=False, indent=2)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
