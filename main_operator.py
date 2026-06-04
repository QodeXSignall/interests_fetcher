from interests_fetcher.interest_merge_funcs import merge_overlapping_interests
from interests_fetcher.cms_interface import functions as cms_api_funcs
from interests_fetcher.functions import parse_interest_name
from interests_fetcher.qt_rm_client import QTRMAsyncClient
from interests_fetcher import functions as main_funcs
from interests_fetcher import cloud_uploader
from interests_fetcher.new_alg import adapter as new_alg_adapter
import time

from interests_fetcher.logger import logger, pipeline_event
from interests_fetcher.data import settings
from interests_fetcher import cms_gate_client
from interests_fetcher import video_utils
from interests_fetcher.vehicle_sync import run_periodic_trucks_sync_after_delay, sync_trucks_from_gate, trucks_sync_interval_sec
import posixpath
import traceback
import datetime
import asyncio
import shutil
import os
import httpx


class Main:
    def __init__(self, output_format="mp4"):
        #threading.Thread(target=main_funcs.video_remover_cycle).start()
        self.output_format = output_format
        self.devices_in_progress = []
        self.TIME_FMT = "%Y-%m-%d %H:%M:%S"
        self._global_interests_sem = None
        self._per_device_sem = {}
        self._devices_sem = None
        self._last_devices_online = []
        self._interest_refill_in_progress = set()
        self.qt_rm_client = QTRMAsyncClient(
            base_url=settings.qt_rm_url,
            username=settings.qt_rm_login,
            password=settings.qt_rm_password,
            concurrent_requests=settings.config.getint("QT_RM", "CONCURRENT_REQUESTS", fallback=16),)

    def _get_global_sem(self):
        if self._global_interests_sem is None:
            self._global_interests_sem = asyncio.Semaphore(settings.config.getint("Process", "MAX_GLOBAL_INTERESTS"))
        return self._global_interests_sem

    def _get_devices_sem(self):
        if self._devices_sem is None:
            self._devices_sem = asyncio.Semaphore(settings.config.getint("Process", "MAX_DEVICES_CONCURRENT"))
        return self._devices_sem

    def _get_device_sem(self, reg_id):
        sem = self._per_device_sem.get(reg_id)
        if sem is None:
            sem = asyncio.Semaphore(settings.config.getint("Process", "MAX_INTERESTS_PER_DEVICE"))
            self._per_device_sem[reg_id] = sem
        return sem

    def _detect_and_upsert_gaps(
        self,
        *,
        reg_id: str,
        tracks: list,
        window_start: str,
        window_end: str,
    ) -> None:
        """
        Прогоняет `detect_gaps` по tracks в окне [window_start, window_end] и
        пушит найденные разрывы в states.json через `upsert_gap`.

        Вызывается изнутри `get_interests_async` при успешном результате, т.е.
        один раз на успешный forward-запрос треков. Сам по себе НЕ фильтрует
        выгрузку — только фиксирует дыры (Этап 1 — observability).
        """
        if not tracks:
            return

        try:
            detected = cms_api_funcs.detect_gaps(
                tracks=tracks,
                window_start=window_start,
                window_end=window_end,
                reg_id=reg_id,
            )
        except Exception:
            logger.exception(f"{reg_id}: detect_gaps failed")
            return

        if not detected:
            return

        for g in detected:
            main_funcs.upsert_gap(
                reg_id=reg_id,
                gap_start=g["gap_start"],
                gap_end=g["gap_end"],
            )

        logger.info(
            f"{reg_id}: detect_gaps в окне [{window_start} → {window_end}] -> "
            f"найдено {len(detected)} разрыв(ов)"
        )

    def _recompute_gap_links(self, reg_id: str) -> None:
        """
        Пересчитывает связи между `pending` gap'ами и pending_interests
        регистратора: обновляет `linked_interest_names` у gap'ов и
        `blocking_gap_ids` у интересов.

        Правило пересечения: gap пересекается с интересом, если
            gap_end   >= interest.start - BUFFER
            AND gap_start <= interest.end   + BUFFER
        где BUFFER = PUBLICATION_GAP_BUFFER_MIN минут.

        Этап 1: блокировка выгрузки ещё не включена — пишем только поля.
        """
        gaps = main_funcs.get_gaps(reg_id)
        pending = main_funcs.get_pending_interests(reg_id)
        if not gaps and not pending:
            return

        buffer_min = settings.config.getint(
            "Interests", "PUBLICATION_GAP_BUFFER_MIN", fallback=5
        )
        buffer = datetime.timedelta(minutes=buffer_min)
        TIME_FMT = self.TIME_FMT

        pending_parsed: list[tuple[str, datetime.datetime, datetime.datetime]] = []
        for it in pending:
            if not isinstance(it, dict):
                continue
            nm = it.get("name")
            st = it.get("start_time")
            en = it.get("end_time")
            if not (nm and st and en):
                continue
            try:
                s_dt = datetime.datetime.strptime(st, TIME_FMT)
                e_dt = datetime.datetime.strptime(en, TIME_FMT)
            except Exception:
                continue
            pending_parsed.append((nm, s_dt, e_dt))

        blocking_by_name: dict[str, list[str]] = {nm: [] for nm, _, _ in pending_parsed}

        for g in gaps:
            if not isinstance(g, dict) or g.get("status") != "pending":
                continue
            gid = g.get("id")
            try:
                gs = datetime.datetime.strptime(g.get("gap_start", ""), TIME_FMT)
                ge = datetime.datetime.strptime(g.get("gap_end", ""), TIME_FMT)
            except Exception:
                continue

            linked_names: list[str] = []
            for nm, s_dt, e_dt in pending_parsed:
                if ge >= (s_dt - buffer) and gs <= (e_dt + buffer):
                    linked_names.append(nm)
                    if gid:
                        blocking_by_name[nm].append(gid)

            if sorted(g.get("linked_interest_names") or []) != sorted(linked_names):
                main_funcs.update_gap(reg_id, gid, linked_interest_names=linked_names)

        if blocking_by_name:
            main_funcs.set_blocking_gap_ids(reg_id, blocking_by_name)

    def _cluster_gaps_for_recheck(
        self,
        gaps: list[dict],
        context_min: int,
        now: datetime.datetime,
    ) -> list[dict]:
        """
        Группирует gap'ы в кластеры, где "жирные" query-окна
        (`[gap_start - context, gap_end + context]`) пересекаются.

        Возвращает:
            [
                {
                    "query_start": "YYYY-... HH:...",
                    "query_end":   "YYYY-... HH:...",
                    "gaps": [gap_dict, ...],
                },
                ...
            ]

        query_end никогда не выходит за `now` (не идём в будущее).
        """
        if not gaps:
            return []
        context = datetime.timedelta(minutes=context_min)
        items: list[tuple[datetime.datetime, datetime.datetime, dict]] = []
        for g in gaps:
            try:
                gs = datetime.datetime.strptime(g["gap_start"], self.TIME_FMT)
                ge = datetime.datetime.strptime(g["gap_end"], self.TIME_FMT)
            except Exception:
                continue
            q_start = gs - context
            q_end = min(ge + context, now)
            if q_end <= q_start:
                continue
            items.append((q_start, q_end, g))
        items.sort(key=lambda x: x[0])
        if not items:
            return []

        clusters: list[dict] = []
        cur_start, cur_end, first_g = items[0]
        cur_gaps = [first_g]
        for q_start, q_end, g in items[1:]:
            if q_start <= cur_end:
                if q_end > cur_end:
                    cur_end = q_end
                cur_gaps.append(g)
            else:
                clusters.append({
                    "query_start": cur_start.strftime(self.TIME_FMT),
                    "query_end": cur_end.strftime(self.TIME_FMT),
                    "gaps": cur_gaps,
                })
                cur_start, cur_end, cur_gaps = q_start, q_end, [g]
        clusters.append({
            "query_start": cur_start.strftime(self.TIME_FMT),
            "query_end": cur_end.strftime(self.TIME_FMT),
            "gaps": cur_gaps,
        })
        return clusters

    def _count_tracks_in_window(
        self,
        tracks: list,
        window_start: datetime.datetime,
        window_end: datetime.datetime,
    ) -> int:
        """Считает кол-во треков, попадающих в [window_start, window_end] (inclusive)."""
        n = 0
        for t in tracks or []:
            gt = t.get("gt") if isinstance(t, dict) else None
            if not gt:
                continue
            try:
                t_dt = datetime.datetime.strptime(gt, self.TIME_FMT)
            except Exception:
                continue
            if window_start <= t_dt <= window_end:
                n += 1
        return n

    def _build_fallback_interests_for_gap(
        self,
        *,
        reg_id: str,
        reg_cfg: dict,
        gap: dict,
        prepared_alarms: dict | list | None,
    ) -> list[dict]:
        """
        Этап 2.5: собирает fallback-интересы из алармов, попавших в abandoned
        gap, когда нормально дождаться треков не получилось.

        Правила:
          * включено только если `GAP_ALARM_FALLBACK_ENABLED`;
          * берём алармы из `prepared_alarms["alarms"]`, у которых
            `start_str` внутри [gap.gap_start, gap.gap_end];
          * фильтр по скорости: `ssp` (десятые) >= GAP_ALARM_FALLBACK_MAX_SPEED_TENTH -> skip;
            дополнительно `start_stopped == True`;
          * фильтр по cargo_type: 'unknown' -> skip;
          * фильтр по ignore_points (депо/МПЗ) по координатам аларма;
          * окно каждого аларма: [start - BEFORE_SEC, end + AFTER_SEC], либо
            при отсутствии end -> [start - BEFORE_SEC, start + NOEND_AFTER_SEC];
          * близкие окна (расстояние <= MERGE_GAP_SEC) объединяются в один
            fallback-интерес.

        Возвращает список готовых dict'ов интересов (в формате, совместимом с
        `append_pending_interests`); список может быть пустым.
        """
        cfg = settings.config
        if not cfg.getboolean("Interests", "GAP_ALARM_FALLBACK_ENABLED", fallback=True):
            return []

        before_sec = cfg.getint("Interests", "GAP_ALARM_FALLBACK_BEFORE_SEC", fallback=120)
        after_sec = cfg.getint("Interests", "GAP_ALARM_FALLBACK_AFTER_SEC", fallback=120)
        noend_after_sec = cfg.getint("Interests", "GAP_ALARM_FALLBACK_NOEND_AFTER_SEC", fallback=300)
        merge_gap_sec = cfg.getint("Interests", "GAP_ALARM_FALLBACK_MERGE_GAP_SEC", fallback=30)
        max_speed_tenth = cfg.getint("Interests", "GAP_ALARM_FALLBACK_MAX_SPEED_TENTH", fallback=3)
        ignore_tol = cfg.getint("Interests", "IGNORE_POINTS_TOLERANCE", fallback=0)
        photo_after_shift_sec = cfg.getint("Interests", "PHOTO_AFTER_SHIFT_SEC", fallback=13)

        if isinstance(prepared_alarms, dict):
            alarms_list = prepared_alarms.get("alarms") or []
        elif isinstance(prepared_alarms, list):
            alarms_list = prepared_alarms
        else:
            alarms_list = []
        if not alarms_list:
            return []

        try:
            gap_s_dt = datetime.datetime.strptime(gap["gap_start"], self.TIME_FMT)
            gap_e_dt = datetime.datetime.strptime(gap["gap_end"], self.TIME_FMT)
        except Exception:
            return []

        try:
            from interests_fetcher import geo_funcs
            ignore_points = geo_funcs.get_ignore_points()
        except Exception:
            ignore_points = []

        def _ignored_geo(geo_str: str | None) -> str | None:
            if not geo_str or not ignore_points or ignore_tol <= 0:
                return None
            try:
                from interests_fetcher import geo_funcs
                return geo_funcs.find_nearby_name(geo_str, ignore_points, ignore_tol)
            except Exception:
                return None

        plate = (reg_cfg or {}).get("plate") or reg_id

        candidates: list[dict] = []
        for a in alarms_list:
            if not isinstance(a, dict):
                continue
            try:
                a_start_dt = a.get("start_dt") or datetime.datetime.strptime(a.get("start_str", ""), self.TIME_FMT)
                a_end_dt = a.get("end_dt")
                if not a_end_dt and a.get("end_str"):
                    a_end_dt = datetime.datetime.strptime(a["end_str"], self.TIME_FMT)
            except Exception:
                continue
            if a_start_dt < gap_s_dt or a_start_dt > gap_e_dt:
                continue

            cargo = a.get("cargo_type")
            if not cargo or cargo == "unknown":
                continue

            ssp_kmh = a.get("ssp_kmh")
            if ssp_kmh is None:
                ssp_kmh = (a.get("ssp") or 0) / 10.0
            if ssp_kmh * 10.0 >= max_speed_tenth:
                continue
            if a.get("start_stopped") is False:
                continue

            slng = a.get("slng")
            slat = a.get("slat")
            geo_str = None
            if slng is not None and slat is not None:
                geo_str = f"{slng},{slat}"
            zone = _ignored_geo(geo_str)
            if zone:
                logger.info(
                    f"{reg_id}: [FALLBACK] аларм {a.get('start_str')} в зоне игнора '{zone}' — skip"
                )
                continue

            if a_end_dt and a_end_dt > a_start_dt:
                w_start = a_start_dt - datetime.timedelta(seconds=before_sec)
                w_end = a_end_dt + datetime.timedelta(seconds=after_sec)
            else:
                w_start = a_start_dt - datetime.timedelta(seconds=before_sec)
                w_end = a_start_dt + datetime.timedelta(seconds=noend_after_sec)

            candidates.append({
                "w_start": w_start,
                "w_end": w_end,
                "alarm": a,
                "cargo": cargo,
                "geo": geo_str,
            })

        if not candidates:
            return []

        candidates.sort(key=lambda c: c["w_start"])
        clusters: list[dict] = []
        for c in candidates:
            if not clusters:
                clusters.append({
                    "w_start": c["w_start"],
                    "w_end": c["w_end"],
                    "alarms": [c["alarm"]],
                    "cargos": {c["cargo"]},
                    "geo": c["geo"],
                })
                continue
            last = clusters[-1]
            if (c["w_start"] - last["w_end"]).total_seconds() <= merge_gap_sec:
                if c["w_end"] > last["w_end"]:
                    last["w_end"] = c["w_end"]
                last["alarms"].append(c["alarm"])
                last["cargos"].add(c["cargo"])
                if not last["geo"] and c["geo"]:
                    last["geo"] = c["geo"]
            else:
                clusters.append({
                    "w_start": c["w_start"],
                    "w_end": c["w_end"],
                    "alarms": [c["alarm"]],
                    "cargos": {c["cargo"]},
                    "geo": c["geo"],
                })

        def _cargo_to_human(cargos: set[str]) -> str:
            if "kgo" in cargos and "euro" not in cargos:
                return "Бункер"
            if "euro" in cargos and "kgo" not in cargos:
                return "Контейнер"
            return "Контейнер"

        fallback_interests: list[dict] = []
        gap_id = gap.get("id")
        for cl in clusters:
            s_dt: datetime.datetime = cl["w_start"]
            e_dt: datetime.datetime = cl["w_end"]
            s_str = s_dt.strftime(self.TIME_FMT)
            e_str = e_dt.strftime(self.TIME_FMT)
            photo_before = s_str
            photo_after_dt = e_dt - datetime.timedelta(seconds=photo_after_shift_sec)
            if photo_after_dt < s_dt:
                photo_after_dt = s_dt
            photo_after = photo_after_dt.strftime(self.TIME_FMT)

            cargo_human = _cargo_to_human(cl["cargos"])

            switch_events = []
            for a in cl["alarms"]:
                switch_events.append({
                    "datetime": a.get("start_str"),
                    "switch": a.get("io_index"),
                    "source": "alarm-abandoned-gap",
                })

            def _secs(d: datetime.datetime) -> int:
                return d.hour * 3600 + d.minute * 60 + d.second

            interest = {
                "name": (
                    f"{plate}_"
                    f"{s_dt.year}.{s_dt.month:02d}.{s_dt.day:02d} "
                    f"{s_dt.hour:02d}.{s_dt.minute:02d}.{s_dt.second:02d}-"
                    f"{e_dt.hour:02d}.{e_dt.minute:02d}.{e_dt.second:02d}"
                ),
                "reg_id": reg_id,
                "beg_sec": _secs(s_dt),
                "end_sec": _secs(e_dt),
                "year": s_dt.year,
                "month": s_dt.month,
                "day": s_dt.day,
                "start_time": s_str,
                "end_time": e_str,
                "car_number": plate,
                "photo_before_timestamp": photo_before,
                "photo_after_timestamp": photo_after,
                "photo_before_sec": _secs(s_dt),
                "photo_after_sec": _secs(photo_after_dt),
                "report": {
                    "cargo_type": cargo_human,
                    "geo": cl["geo"],
                    "switches_amount": len(cl["alarms"]),
                    "switch_events": switch_events,
                },
                "source": "abandoned_gap_alarm_fallback",
                "from_abandoned_gap": True,
                "fallback_gap_id": gap_id,
            }
            fallback_interests.append(interest)

        return fallback_interests

    async def _attempt_alarm_fallback_before_abandon(
        self,
        *,
        reg_id: str,
        reg_cfg: dict,
        gap: dict,
        prepared_alarms: dict | list | None,
        tracks: list | None,
        reason: str,
    ) -> bool:
        """
        Перед переводом gap'а в `abandoned` — собираем fallback-интересы из
        алармов (см. `_build_fallback_interests_for_gap`) и кладём их в
        pending_interests. Мердж с уже существующими интересами произойдёт
        автоматически через последующий `merge_overlapping_interests` в
        forward-проходе.

        Если `prepared_alarms` отсутствует (max_age-путь) — подтягиваем их
        свежим запросом через `get_interests_async(return_raw=True)` на
        небольшом окне вокруг gap'а.

        Возвращает True, если хотя бы один fallback-интерес добавлен.
        """
        if not gap:
            return False

        if prepared_alarms is None:
            try:
                gap_s_dt = datetime.datetime.strptime(gap["gap_start"], self.TIME_FMT)
                gap_e_dt = datetime.datetime.strptime(gap["gap_end"], self.TIME_FMT)
            except Exception:
                logger.warning(f"{reg_id}: fallback: плохие границы gap'а {gap.get('id')}")
                return False
            context_min = settings.config.getint(
                "Interests", "GAP_RECHECK_CONTEXT_MIN", fallback=30
            )
            q_start_dt = gap_s_dt - datetime.timedelta(minutes=context_min)
            q_end_dt = gap_e_dt + datetime.timedelta(minutes=context_min)
            now = datetime.datetime.now()
            if q_end_dt > now:
                q_end_dt = now
            q_start = q_start_dt.strftime(self.TIME_FMT)
            q_end = q_end_dt.strftime(self.TIME_FMT)
            try:
                raw = await self.get_interests_async(
                    reg_id, reg_cfg, q_start, q_end, return_raw=True
                )
            except Exception:
                logger.exception(
                    f"{reg_id}: fallback: get_interests_async failed for gap {gap.get('id')}"
                )
                return False
            if not raw:
                return False
            _interests, tracks, prepared_alarms = raw

        candidates = self._build_fallback_interests_for_gap(
            reg_id=reg_id,
            reg_cfg=reg_cfg,
            gap=gap,
            prepared_alarms=prepared_alarms,
        )
        if not candidates:
            logger.info(
                f"{reg_id}: fallback({reason}): gap {gap.get('id')} "
                f"[{gap.get('gap_start')} → {gap.get('gap_end')}] — подходящих "
                f"алармов не найдено, просто abandoned."
            )
            return False

        try:
            main_funcs.append_pending_interests(reg_id, candidates)
        except Exception:
            logger.exception(
                f"{reg_id}: fallback({reason}): append_pending_interests failed"
            )
            return False

        logger.info(
            f"{reg_id}: fallback({reason}): gap {gap.get('id')} -> добавлено "
            f"{len(candidates)} fallback-интерес(ов) из алармов: "
            f"{[it['name'] for it in candidates]}"
        )
        return True

    async def _process_gaps_if_due(self, reg_id: str) -> None:
        """
        Gap-проход: перебирает pending gap'ы регистратора и:

          1. Если gap старше GAP_MAX_AGE_HOURS -> пытается alarm-fallback
             (Этап 2.5, сейчас no-op) и абандонит. Запрос треков НЕ делается.
          2. Для остальных due-gap'ов (last_checked is None или прошло
             GAP_RECHECK_INTERVAL_HOURS) строит кластеры (см.
             `_cluster_gaps_for_recheck`) и для каждого:
               - делает `get_interests_async(return_raw=True)` на query-окне;
               - прогоняет найденные интересы через `_sync_recheck_with_cloud`;
               - вызывает `reconcile_gaps_in_window` для сверки старых
                 pending gap'ов в окне с новым результатом детектора
                 (CLOSED / SHRUNK / SPLIT / NEW + NO_PROGRESS).
          3. Для gap'ов, у которых `no_progress_streak` достиг порога —
             пробует alarm-fallback и переводит в abandoned.

        В конце — пересчитывает gap<->interest связи через `_recompute_gap_links`.
        """
        now = datetime.datetime.now()
        recheck_interval_h = settings.config.getint(
            "Interests", "GAP_RECHECK_INTERVAL_HOURS", fallback=4
        )
        max_age_h = settings.config.getint(
            "Interests", "GAP_MAX_AGE_HOURS", fallback=72
        )
        context_min = settings.config.getint(
            "Interests", "GAP_RECHECK_CONTEXT_MIN", fallback=30
        )
        stable_checks = settings.config.getint(
            "Interests", "GAP_STABLE_NO_PROGRESS_CHECKS", fallback=3
        )

        gaps = main_funcs.get_gaps(reg_id)
        pending = [g for g in gaps if isinstance(g, dict) and g.get("status") == "pending"]
        if not pending:
            return

        to_abandon_max_age: list[dict] = []
        due_list: list[dict] = []
        for g in pending:
            created_at = g.get("created_at")
            max_aged = False
            if created_at:
                try:
                    ca_dt = datetime.datetime.strptime(created_at, self.TIME_FMT)
                    if (now - ca_dt).total_seconds() >= max_age_h * 3600:
                        to_abandon_max_age.append(g)
                        max_aged = True
                except Exception:
                    pass
            if max_aged:
                continue

            last_checked = g.get("last_checked")
            if not last_checked:
                due_list.append(g)
                continue
            try:
                lc_dt = datetime.datetime.strptime(last_checked, self.TIME_FMT)
                if (now - lc_dt).total_seconds() >= recheck_interval_h * 3600:
                    due_list.append(g)
            except Exception:
                due_list.append(g)

        reg_cfg = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate=None)

        for g in to_abandon_max_age:
            logger.info(
                f"{reg_id}: gap id={g.get('id')} [{g.get('gap_start')} → {g.get('gap_end')}] "
                f"MAX_AGE (>{max_age_h}h) -> attempt fallback & abandon"
            )
            try:
                await self._attempt_alarm_fallback_before_abandon(
                    reg_id=reg_id,
                    reg_cfg=reg_cfg,
                    gap=g,
                    prepared_alarms=None,
                    tracks=None,
                    reason="max_age",
                )
            except Exception:
                logger.exception(f"{reg_id}: alarm-fallback(max_age) failed for gap {g.get('id')}")
            main_funcs.close_gap(reg_id, g.get("id"), abandoned=True)

        if not due_list:
            self._recompute_gap_links(reg_id)
            return

        clusters = self._cluster_gaps_for_recheck(due_list, context_min, now)

        for cluster in clusters:
            q_start = cluster["query_start"]
            q_end = cluster["query_end"]
            cluster_gaps = cluster["gaps"]

            logger.info(
                f"{reg_id}: GAP RECHECK cluster [{q_start} → {q_end}], "
                f"gaps_in_cluster={len(cluster_gaps)}"
            )

            try:
                raw = await self.get_interests_async(
                    reg_id, reg_cfg, q_start, q_end, return_raw=True
                )
            except Exception:
                logger.exception(f"{reg_id}: GAP RECHECK get_interests_async failed")
                continue

            if not raw:
                continue
            new_interests, tracks, prepared_alarms = raw

            if new_interests:
                self._log_interests_shape(reg_id, "GAP_RECHECK_BEFORE_MERGE", new_interests)
                merged = self._merge_interests_safe(reg_id, "GAP_RECHECK_BEFORE_MERGE", new_interests)
                if merged:
                    try:
                        await self._sync_recheck_with_cloud(
                            reg_id=reg_id,
                            recheck_interests=merged,
                            st=q_start,
                            en=q_end,
                            time_fmt=self.TIME_FMT,
                        )
                    except Exception:
                        logger.exception(f"{reg_id}: GAP RECHECK _sync_recheck_with_cloud failed")

            if tracks is None:
                logger.warning(
                    f"{reg_id}: GAP RECHECK cluster [{q_start} → {q_end}] -> "
                    f"tracks=None (LoadingInProgress или ошибка) — reconcile пропускаем."
                )
                continue

            try:
                detected = cms_api_funcs.detect_gaps(
                    tracks=tracks,
                    window_start=q_start,
                    window_end=q_end,
                    reg_id=reg_id,
                )
            except Exception:
                logger.exception(f"{reg_id}: GAP RECHECK detect_gaps failed")
                detected = []

            points_per_gap: dict[str, int] = {}
            context_td = datetime.timedelta(minutes=context_min)
            for g in cluster_gaps:
                try:
                    gs_dt = datetime.datetime.strptime(g["gap_start"], self.TIME_FMT)
                    ge_dt = datetime.datetime.strptime(g["gap_end"], self.TIME_FMT)
                except Exception:
                    continue
                q_gs = gs_dt - context_td
                q_ge = min(ge_dt + context_td, now)
                gid = g.get("id")
                if gid:
                    points_per_gap[gid] = self._count_tracks_in_window(tracks, q_gs, q_ge)

            recon = main_funcs.reconcile_gaps_in_window(
                reg_id=reg_id,
                detected_gaps=detected,
                window_start=q_start,
                window_end=q_end,
                points_per_gap=points_per_gap,
                stable_checks_threshold=stable_checks,
            )

            for gid in recon.get("no_progress", []):
                target = None
                for g in main_funcs.get_gaps(reg_id):
                    if isinstance(g, dict) and g.get("id") == gid:
                        target = g
                        break
                if target is None or target.get("status") != "pending":
                    continue
                logger.info(
                    f"{reg_id}: gap id={gid} NO_PROGRESS (streak>={stable_checks}) -> "
                    f"attempt fallback & abandon"
                )
                try:
                    await self._attempt_alarm_fallback_before_abandon(
                        reg_id=reg_id,
                        reg_cfg=reg_cfg,
                        gap=target,
                        prepared_alarms=prepared_alarms,
                        tracks=tracks,
                        reason="no_progress",
                    )
                except Exception:
                    logger.exception(f"{reg_id}: alarm-fallback(no_progress) failed for gap {gid}")
                main_funcs.close_gap(reg_id, gid, abandoned=True)

        self._recompute_gap_links(reg_id)

    async def _sync_recheck_with_cloud(
        self,
        reg_id: str,
        recheck_interests: list[dict],
        st: str,
        en: str,
        time_fmt: str,
    ) -> None:
        """
        Сравнивает интересы, найденные при recheck, с тем, что уже лежит на WebDAV:

          - берём интересы на облаке в окне [st, en]
          - фаззи-сравниваем их с recheck_interests
          - новые (detected, которых нет на облаке) -> append_pending_interests
          - устаревшие (на облаке, но нет в detected) -> удаляем с облака
        """

        if not recheck_interests:
            return

        # Окно для recheck
        try:
            window_start = datetime.datetime.strptime(st, time_fmt)
            window_end = datetime.datetime.strptime(en, time_fmt)
        except Exception as e:
            logger.warning(f"{reg_id}: Не удалось распарсить окно recheck [{st} → {en}]: {e}")
            return

        # Пытаемся достать plate. Надёжнее всего — из имени интереса.
        plate = None
        for it in recheck_interests:
            nm = (it or {}).get("name")
            if not nm:
                continue
            try:
                p, _, _ = main_funcs._interest_name_to_interval(nm)
                plate = p
                break
            except Exception:
                continue

        if not plate:
            # fallback: пробуем из states.json
            reg_cfg = main_funcs.get_reg_info(reg_id)
            plate = (reg_cfg or {}).get("plate")

        if not plate:
            logger.warning(f"{reg_id}: Не удалось определить plate для recheck-синхронизации. Пропускаем.")
            return

        client = cloud_uploader.client

        # Собираем имена интересов на облаке, которые попадают в окно [window_start, window_end]
        expected_names: set[str] = set()

        # С небольшим запасом по датам, чтобы не откусить края
        day = window_start.date()
        last_day = window_end.date()
        margin_sec = 120  # запас по секундам при отборе интересов по окну

        while day <= last_day:
            day_str = day.strftime("%Y.%m.%d")
            cloud_names = cloud_uploader._list_cloud_interest_folders_for_day(client, plate, day_str)
            for name in cloud_names:
                try:
                    _, s_dt, e_dt = main_funcs._interest_name_to_interval(name)
                except Exception:
                    continue
                # отсекаем интересы, которые явно вне окна recheck, с запасом
                if e_dt < (window_start - datetime.timedelta(seconds=margin_sec)):
                    continue
                if s_dt > (window_end + datetime.timedelta(seconds=margin_sec)):
                    continue
                expected_names.add(name)
            day += datetime.timedelta(days=1)

        detected_names: set[str] = set()
        for it in recheck_interests:
            nm = (it or {}).get("name")
            if nm:
                detected_names.add(nm)

        new_exact, missing_exact = main_funcs.exact_diff_sets(expected_names, detected_names)

        # 2. Новые интересы → в pending_interests
        if new_exact:
            raw_to_append = [it for it in recheck_interests if it.get("name") in new_exact]
            to_append: list[dict] = []
            for it in raw_to_append:
                name = it.get("name")
                if not name:
                    continue
                if await self._interest_exists_in_cloud(name):
                    logger.info(f"{reg_id}: RECHECK: {name} уже есть в облаке, пропускаем добавление в pending.")
                    continue
                to_append.append(it)
            if to_append:
                logger.info(
                    f"{reg_id}: RECHECK: обнаружено {len(to_append)} новых интересов по сравнению с WebDAV "
                    f"(будут добавлены в pending_interests)."
                )
                main_funcs.append_pending_interests(reg_id, to_append)

        # 3. Устаревшие интересы → удалить с облака
        for name in sorted(missing_exact):
            try:
                plate2, s_dt, _ = main_funcs._interest_name_to_interval(name)
            except Exception:
                logger.warning(f"{reg_id}: RECHECK: не удалось распарсить имя устаревшего интереса '{name}' — пропуск.")
                continue

            day_str = s_dt.strftime("%Y.%m.%d")
            folder_path = f"{settings.CLOUD_PATH}/{plate2}/{day_str}/{name}"

            try:
                logger.info(f"{reg_id}: RECHECK: удаляем устаревший интерес с WebDAV: {folder_path}")
                client.clean(folder_path)
            except cloud_uploader.RemoteResourceNotFound:
                logger.info(f"{reg_id}: RECHECK: папка интереса уже отсутствует: {folder_path}")
            except Exception as e:
                logger.error(f"{reg_id}: RECHECK: ошибка при удалении интереса '{name}' из WebDAV: {e}")


    async def _interest_exists_in_cloud(self, interest_name: str) -> bool:
        """
        Осторожно проверяет наличие папки интереса в облаке с кэшем WebDAV.
        """
        try:
            _, _, folder_path = cloud_uploader.get_interest_folder_path(interest_name, settings.CLOUD_PATH)
        except Exception as e:
            logger.warning(f"Не удалось получить путь интереса '{interest_name}' для проверки на облаке: {e}")
            return False

        try:
            return await cloud_uploader.cached_check(cloud_uploader.client, folder_path)
        except Exception as e:
            logger.warning(f"[WEBDAV] Ошибка cached_check для '{folder_path}': {e}")
            return False


    async def get_devices_online(self):
        try:
            devices = await cms_gate_client.list_devices(status="online", timeout_sec=180.0)
            if devices:
                logger.debug(f"Got devices online from cms_gate: {devices}")
            else:
                logger.debug("No devices online from cms_gate.")
            self._last_devices_online = devices
            logger.info(
                pipeline_event(
                    "devices_online_poll",
                    count=len(devices or []),
                    fallback=False,
                )
            )
            return devices
        except TimeoutError as e:
            fallback_count = len(self._last_devices_online or [])
            logger.warning(
                f"list_devices timeout in cms_gate: {e}. "
                f"Using last known online devices ({fallback_count})."
            )
            logger.warning(
                pipeline_event(
                    "devices_online_poll",
                    count=len(self._last_devices_online or []),
                    fallback=True,
                )
            )
            return self._last_devices_online or []
        except httpx.HTTPError as e:
            # Сетевые ошибки обращения к cms_gate (ConnectError, ReadError,
            # TimeoutException, HTTPStatusError и т.п.) не должны убивать демон —
            # отдаём последний известный список online-устройств как fallback.
            fallback_count = len(self._last_devices_online or [])
            logger.warning(
                f"list_devices network error in cms_gate: {type(e).__name__}: {e}. "
                f"Using last known online devices ({fallback_count})."
            )
            logger.warning(
                pipeline_event(
                    "devices_online_poll",
                    count=fallback_count,
                    fallback=True,
                )
            )
            return self._last_devices_online or []
        except Exception as e:
            fallback_count = len(self._last_devices_online or [])
            logger.exception(
                f"list_devices unexpected error in cms_gate: {e}. "
                f"Using last known online devices ({fallback_count})."
            )
            logger.warning(
                pipeline_event(
                    "devices_online_poll",
                    count=fallback_count,
                    fallback=True,
                )
            )
            return self._last_devices_online or []

    async def operate_device(self, reg_id, plate):
        if reg_id in self.devices_in_progress:
            return
        self.devices_in_progress.append(reg_id)
        try:
            await self.download_reg_videos(reg_id, plate)
        except Exception:
            logger.error(traceback.format_exc())
        finally:
            # гарантированно освобождаем
            if reg_id in self.devices_in_progress:
                self.devices_in_progress.remove(reg_id)

    async def get_interests_async(
        self,
        reg_id,
        reg_info,
        start_time,
        stop_time,
        *,
        return_raw: bool = False,
    ):
        """
        Асинхронная версия получения интересов:
        - CMS треки (queryTrackDetail) — в thread-пуле через asyncio.to_thread
        - CMS alarm detail — в thread-пуле через asyncio.to_thread
        - Подготовка алармов/сшивка — синхронно (CPU), можно оставить в основном потоке
        Логика «шага назад по минуте» (max_extra_pulls) сохранена.

        По умолчанию возвращает список интересов (`list[dict]`) и делает
        forward-`_detect_and_upsert_gaps` по окну запроса.

        При `return_raw=True`:
          - возвращает кортеж `(interests, tracks, prepared_alarms)`
          - gap detection НЕ выполняется (вызывающий отвечает за reconcile).
          - В случае ошибки/пустого результата возвращает `([], None, None)`.
        """
        max_extra_pulls = 8  # максимум шагов назад по минуте
        pulls = 0

        while True:
            start_time_dt = datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            tracks_prealarm_lookback_sec = settings.config.getint(
                "Interests", "TRACKS_PREALARM_LOOKBACK_SEC", fallback=120
            )
            query_start_time = (start_time_dt - datetime.timedelta(seconds=tracks_prealarm_lookback_sec)).strftime(
                self.TIME_FMT
            )

            # Берём треки и алармы через cms_gate (прямые HTTP)
            tracks, all_alarms = await cms_gate_client.get_tracks_and_alarms(
                reg_id=reg_id,
                start_time=query_start_time,
                end_time=stop_time,
            )
            print(f"tracks: {tracks}")

            prepared = cms_api_funcs.prepare_alarms(
                raw_alarms=all_alarms,
                reg_cfg=reg_info,
                allowed_atp=frozenset({19, 20, 21, 22}),
                min_stop_speed_kmh=settings.config.getint("Interests", "MIN_STOP_SPEED") / 10.0,
                merge_gap_sec=15,
                reg_id=reg_id
            )

            # Какой алгоритм отдаём наверх: новый new_alg или legacy
            # find_interests_by_lifting_switches. Управляется флагом
            # [Interests] USE_NEW_ALGORITHM в config.cfg.
            #
            # SHADOW_NEW_ALGORITHM=true — гоняем оба алгоритма и логируем дифф.
            # Не влияет на то, чей результат отдаётся вверх (это решает USE_NEW_ALGORITHM).
            # Работает в обе стороны: и для проверки нового пока legacy основной,
            # и как сейф-нет когда новый уже основной.
            use_new = settings.config.getboolean(
                "Interests", "USE_NEW_ALGORITHM", fallback=False
            )
            shadow_new = settings.config.getboolean(
                "Interests", "SHADOW_NEW_ALGORITHM", fallback=False
            )

            new_interests = None
            new_alg_err = None
            if use_new or shadow_new:
                last_track_dt = None
                if tracks:
                    try:
                        last_track_dt = datetime.datetime.strptime(
                            tracks[-1].get("gt", ""), self.TIME_FMT
                        )
                    except Exception:
                        last_track_dt = None
                try:
                    new_interests = new_alg_adapter.find_interests_legacy_compat(
                        tracks=tracks,
                        raw_alarms=all_alarms,
                        reg_info=reg_info,
                        reg_id=reg_id,
                        last_track_dt=last_track_dt,
                    )
                except cms_api_funcs.LoadingInProgress:
                    new_alg_err = "LoadingInProgress"
                except Exception as e:
                    new_alg_err = f"{type(e).__name__}: {e}"
                    logger.exception(f"{reg_id}: [new_alg] неожиданная ошибка")

            # Legacy запускаем если: (1) он основной, или (2) shadow-замер, или
            # (3) новый упал с неожиданной ошибкой — нужен аварийный fallback.
            need_legacy = (
                (not use_new) or
                shadow_new or
                (new_alg_err is not None and new_alg_err != "LoadingInProgress")
            )
            legacy_interests_obj = None
            if need_legacy:
                try:
                    legacy_interests_obj = cms_api_funcs.find_interests_by_lifting_switches(
                        tracks=tracks,
                        start_tracks_search_time=start_time_dt,
                        reg_id=reg_id,
                        alarms=prepared,
                    )
                except cms_api_funcs.LoadingInProgress:
                    if not use_new or new_alg_err == "LoadingInProgress":
                        # legacy — единственный авторитет, его LoadingInProgress пробрасываем
                        logger.info("Прерываем обработку интересов потому что машина грузится в это время ")
                        if return_raw:
                            return [], None, None
                        return {"error": "Loading in progress"}
                    # use_new=true, shadow=true: legacy ушёл в LoadingInProgress,
                    # но решение принимает new — игнорируем legacy-исключение.
                    legacy_interests_obj = {"interests": []}

            # Решаем что отдавать наверх.
            if use_new and new_alg_err == "LoadingInProgress":
                logger.info("Прерываем обработку интересов потому что машина грузится в это время (new_alg)")
                if return_raw:
                    return [], None, None
                return {"error": "Loading in progress"}

            if use_new and new_alg_err is None:
                interests = {"interests": new_interests or []}
            elif use_new and new_alg_err is not None:
                logger.warning(
                    f"{reg_id}: [new_alg] упал ({new_alg_err}) — fallback на legacy"
                )
                interests = legacy_interests_obj
            else:
                interests = legacy_interests_obj

            # Shadow-замер: дифф «legacy vs new» в логи. Никак не влияет на результат.
            if shadow_new and new_alg_err is None and legacy_interests_obj is not None:
                legacy_list = (
                    legacy_interests_obj.get("interests")
                    if isinstance(legacy_interests_obj, dict) else legacy_interests_obj
                ) or []
                try:
                    diff = new_alg_adapter.diff_for_shadow(legacy_list, new_interests or [])
                    logger.info(
                        f"{reg_id}: [shadow] window={start_time}..{stop_time} "
                        f"legacy={diff['legacy_count']} new={diff['new_count']} "
                        f"common={diff['common']} only_legacy={len(diff['only_legacy'])} "
                        f"only_new={len(diff['only_new'])}"
                    )
                    if diff["only_legacy"]:
                        logger.info(f"{reg_id}: [shadow] only_legacy: {diff['only_legacy']}")
                    if diff["only_new"]:
                        logger.info(f"{reg_id}: [shadow] only_new: {diff['only_new']}")
                except Exception:
                    logger.exception(f"{reg_id}: [shadow] diff failed")

            if isinstance(interests, dict) and "interests" in interests:
                found_interests = interests["interests"]
                if return_raw:
                    return found_interests, tracks, prepared
                try:
                    self._detect_and_upsert_gaps(
                        reg_id=reg_id,
                        tracks=tracks,
                        window_start=start_time,
                        window_end=stop_time,
                    )
                except Exception:
                    logger.exception(f"{reg_id}: gap detection/upsert failed")
                return found_interests

            elif isinstance(interests, dict) and "error" in interests:
                pulls += 1
                if pulls > max_extra_pulls:
                    logger.warning(f"[GUARD] Достигнут предел догрузок (pulls={pulls}). Останавливаемся.")
                    if return_raw:
                        return [], None, None
                    return []
                start_time = (start_time_dt - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"Теперь ищем треки с {start_time}")
                continue

            else:
                logger.warning(f"[ANALYZE] Неожиданный формат из find_interests_by_lifting_switches: {type(interests)}")
                if return_raw:
                    return [], None, None
                return []

    def _parse_start_ts(self, it: dict):
        try:
            return datetime.datetime.strptime(it.get("start_time", ""), settings.TIME_FMT)
        except Exception:
            return datetime.datetime.max  # если испорченный интерес — обрабатываем в самом конце

    @staticmethod
    def _interest_duration_min(it: dict) -> float | None:
        """Длительность интереса в минутах по start_time / end_time. None если не удалось распарсить."""
        try:
            s = datetime.datetime.strptime(it.get("start_time", ""), settings.TIME_FMT)
            e = datetime.datetime.strptime(it.get("end_time", ""), settings.TIME_FMT)
        except Exception:
            return None
        return (e - s).total_seconds() / 60.0

    def _interest_too_long(self, it: dict) -> bool:
        """True если длительность интереса превышает INTEREST_MAX_DURATION_MIN."""
        max_min = settings.config.getint(
            "Interests", "INTEREST_MAX_DURATION_MIN", fallback=40
        )
        dur = self._interest_duration_min(it)
        return dur is not None and dur > max_min

    def _interest_out_of_attempts(self, it: dict) -> bool:
        """True если для интереса уже израсходован бюджет попыток download-clips."""
        max_attempts = settings.config.getint(
            "Interests", "INTEREST_MAX_DOWNLOAD_ATTEMPTS", fallback=5
        )
        attempts = it.get("download_attempts")
        if not isinstance(attempts, int):
            return False
        return attempts >= max_attempts

    def _drop_dead_letter_pending(self, reg_id: str, pending: list[dict]) -> list[dict]:
        """
        Убирает из pending и удаляет из states.json записи, которые:
          - длиннее INTEREST_MAX_DURATION_MIN (мусор, скорее всего alarm-склейка или битый merge);
          - превысили бюджет попыток download-clips (dead-letter).
        Возвращает «живой» остаток pending.
        """
        if not pending:
            return pending
        max_min = settings.config.getint(
            "Interests", "INTEREST_MAX_DURATION_MIN", fallback=40
        )
        max_attempts = settings.config.getint(
            "Interests", "INTEREST_MAX_DOWNLOAD_ATTEMPTS", fallback=5
        )
        survivors: list[dict] = []
        for it in pending:
            if not isinstance(it, dict):
                continue
            name = it.get("name") or "?"
            if self._interest_too_long(it):
                dur = self._interest_duration_min(it)
                logger.warning(
                    f"{reg_id}: dead-letter (too_long): выбрасываем {name!r} "
                    f"длительность={dur:.1f}мин > {max_min}мин "
                    f"[{it.get('start_time')} .. {it.get('end_time')}]"
                )
                logger.info(
                    pipeline_event(
                        "interest_dead_letter",
                        reg_id=str(reg_id),
                        interest=str(name),
                        reason="too_long",
                        duration_min=round(dur, 2) if dur is not None else None,
                        max_duration_min=max_min,
                    )
                )
                self.del_pending_interest(reg_id, it)
                continue
            if self._interest_out_of_attempts(it):
                attempts = it.get("download_attempts", 0)
                logger.warning(
                    f"{reg_id}: dead-letter (out_of_attempts): выбрасываем {name!r} "
                    f"download_attempts={attempts} >= {max_attempts}"
                )
                logger.info(
                    pipeline_event(
                        "interest_dead_letter",
                        reg_id=str(reg_id),
                        interest=str(name),
                        reason="out_of_attempts",
                        attempts=attempts,
                        max_attempts=max_attempts,
                    )
                )
                self.del_pending_interest(reg_id, it)
                continue
            survivors.append(it)
        return survivors

    def _log_interests_shape(self, reg_id: str, stage: str, interests):
        sample_type = "n/a"
        sample_repr = "n/a"
        if isinstance(interests, list):
            if interests:
                sample_type = type(interests[0]).__name__
                sample_repr = repr(interests[0])[:600]
        else:
            sample_type = type(interests).__name__
            sample_repr = repr(interests)[:600]
        try:
            size = len(interests)
        except Exception:
            size = "n/a"
        logger.info(
            f"{reg_id}: [{stage}] interests_shape type={type(interests).__name__} "
            f"size={size} sample_type={sample_type} sample={sample_repr}"
        )

    def _merge_interests_safe(self, reg_id: str, stage: str, raw) -> list[dict] | None:
        """
        Приводит ответ get_interests_async к списку и вызывает merge_overlapping_interests.

        None — прервать только refill: CMS вернул «Loading in progress» (нельзя вызывать merge по dict).
        Пустой список — интересов нет.
        """
        if isinstance(raw, dict) and raw.get("error") == "Loading in progress":
            logger.info(f"{reg_id}: [{stage}] пропуск merge: машина грузится (Loading in progress)")
            return None
        if raw is None or raw == []:
            return []
        if isinstance(raw, dict):
            if "error" in raw:
                logger.warning(f"{reg_id}: [{stage}] ответ с полем error: {raw!r}")
                return []
            logger.warning(f"{reg_id}: [{stage}] неожиданный dict вместо списка интересов: {raw!r}")
            return []
        if not isinstance(raw, list):
            logger.warning(f"{reg_id}: [{stage}] неожиданный тип {type(raw).__name__}")
            return []
        cleaned = [x for x in raw if isinstance(x, dict)]
        if len(cleaned) != len(raw):
            logger.warning(f"{reg_id}: [{stage}] отфильтрованы не-dict элементы в списке интересов")
        if not cleaned:
            return []
        merged = merge_overlapping_interests(cleaned)
        # Отсекаем интересы длиннее INTEREST_MAX_DURATION_MIN (обычно это
        # слипшиеся alarm-монстры или сутки-в-одном-интересе). До pending такие
        # доходить не должны: CMS их не отдаёт, они блокируют очередь.
        max_min = settings.config.getint(
            "Interests", "INTEREST_MAX_DURATION_MIN", fallback=40
        )
        survivors: list[dict] = []
        dropped = 0
        for it in merged or []:
            dur = self._interest_duration_min(it)
            if dur is not None and dur > max_min:
                dropped += 1
                logger.warning(
                    f"{reg_id}: [{stage}] отбрасываем слишком длинный интерес "
                    f"{it.get('name')!r} длительность={dur:.1f}мин > {max_min}мин "
                    f"[{it.get('start_time')} .. {it.get('end_time')}]"
                )
                logger.info(
                    pipeline_event(
                        "interest_dropped_pre_pending",
                        reg_id=str(reg_id),
                        stage=str(stage),
                        interest=str(it.get("name") or ""),
                        reason="too_long",
                        duration_min=round(dur, 2),
                        max_duration_min=max_min,
                    )
                )
                continue
            survivors.append(it)
        if dropped:
            logger.info(
                f"{reg_id}: [{stage}] санитарный фильтр длины: оставили {len(survivors)}, выкинули {dropped}"
            )
        return survivors

    async def download_reg_videos(self, reg_id, plate):
        logger.debug(f"{reg_id}. Начинаем работу с устройством.")

        # Информация о регистраторе
        reg_info = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate)
        logger.debug(f"{reg_id}. Информация о регистраторе: {reg_info}.")

        ignore = reg_info.get("ignore", False)
        if ignore:
            logger.debug(f"{reg_id}. Игнорируем регистратор, поскольку в states.json параметр ignore=true.")
            return

        logger.info(
            pipeline_event(
                "device_run_start",
                reg_id=str(reg_id),
                plate=str(plate or ""),
            )
        )

        # Pending - уже извлеченные из CMS и сохраненные в states.json интересы
        pending = main_funcs.get_pending_interests(reg_id)
        if not pending:
            await self._refill_pending_interests_if_due(reg_id)     # Извлечь новые интересы из CMS
            pending = main_funcs.get_pending_interests(reg_id)

        # Санитарный фильтр: отсекаем и сразу удаляем из states.json мусорные
        # интересы (слишком длинные) и те, что уже израсходовали бюджет попыток.
        # Делается ДО pessimistic/merge, иначе один такой «монстр» может
        # стоять головой очереди бесконечно.
        pending = self._drop_dead_letter_pending(reg_id, pending)

        # --- Этап 3: pessimistic publication ---
        # Отсекаем интересы, у которых blocking_gap_ids непустой: внутри/рядом
        # с интересом есть активный (pending) gap, и мы ждём треков. Такие
        # интересы не уходят ни в merge (чтобы не слиться с разблокированными),
        # ни в батч выгрузки. Они дождутся reconcile (CLOSED/SHRUNK) или
        # abandon (Этап 2.5) и разблокируются.
        if pending:
            total_pending = len(pending)
            unblocked: list[dict] = []
            blocked_names: list[tuple[str, list[str]]] = []
            for it in pending:
                if not isinstance(it, dict):
                    continue
                bl = it.get("blocking_gap_ids") or []
                if bl:
                    blocked_names.append((it.get("name") or "", list(bl)))
                else:
                    unblocked.append(it)
            if blocked_names:
                logger.info(
                    f"{reg_id}: pessimistic: пропускаем {len(blocked_names)} из {total_pending} "
                    f"pending_interests — заблокированы активными gap'ами. "
                    f"Примеры: {blocked_names[:3]}"
                )
            pending = unblocked

        if pending:
            interests = pending
            logger.info(f"{reg_id}: Берём {len(interests)} интерес(а/ов) из очереди pending_interests.")
            logger.debug(f"({interests})")
        else:
            logger.info(f"{reg_id}: Очередь pending_interests пуста (или всё заблокировано активными gap'ами) — завершаем обход.")
            return True

        logger.info(f"{reg_id}: Найдено {len(interests)} интересов")
        self._log_interests_shape(reg_id, "DOWNLOAD_REG_BEFORE_MERGE", interests)
        merged = self._merge_interests_safe(reg_id, "DOWNLOAD_REG_BEFORE_MERGE", interests)
        if merged is None:
            logger.warning(
                f"{reg_id}: pending дал Loading in progress — пропускаем обход (повторим позже)."
            )
            return True
        if not merged:
            logger.warning(f"{reg_id}: после merge список интересов пуст — завершаем обход.")
            return True
        interests = merged
        logger.info(f"{reg_id}: К запуску {len(interests)} интересов (после фильтра processed).")

        # сортируем интересы по времени начала, старые сначала
        interests.sort(key=self._parse_start_ts)

        total_found = len(interests)
        max_per_batch = settings.config.getint("Interests", "MAX_INTERESTS_PER_BATCH", fallback=8)
        if total_found > max_per_batch:
            logger.info(
                f"{reg_id}: Берём в работу только {max_per_batch} из {total_found} интересов (батч). "
                f"Остальные — в следующий цикл."
            )
            interests = interests[:max_per_batch]
        else:
            logger.info(f"{reg_id}: Влезают все интересы ({total_found}) в одну пачку.")

        logger.info(
            pipeline_event(
                "batch_scheduled",
                reg_id=str(reg_id),
                interests=len(interests),
                batch_max=max_per_batch,
            )
        )

        # Стартуем задачи (сами ограничители внутри)
        channel_id = reg_info.get("chanel_id")
        tasks = [asyncio.create_task(self._process_one_interest(it, channel_id)) for it in interests]

        # Собираем результаты по мере готовности
        end_times: list[str] = []
        try:
            for coro in asyncio.as_completed(tasks):
                try:
                    et = await coro
                    if et:
                        end_times.append(et)
                except Exception:
                    logger.error(f"{reg_id}: Ошибка в задаче интереса:\n{traceback.format_exc()}")
        finally:
            # на всякий случай — чтобы не остались висячие задачи
            for t in tasks:
                if not t.done():
                    t.cancel()
        logger.info(f"{reg_id}: Пакет интересов завершён: {len(end_times)}/{len(interests)}")
        logger.info(
            pipeline_event(
                "batch_complete",
                reg_id=str(reg_id),
                tasks=len(interests),
                completed_with_end_time=len(end_times),
            )
        )


    async def _process_one_interest(self, interest: dict, channel_id) -> str | None:
        reg_id = interest.get("reg_id")
        # Ограничители глобально и по устройству
        async with self._get_global_sem(), self._get_device_sem(reg_id):
            created_start_time = datetime.datetime.now()
            interest_name = interest["name"]

            logger.info(f"{reg_id}: Начинаем работу с интересом {interest_name}")
            logger.debug(f"{interest}")

            # Создаём пути в облаке под интерес
            cloud_paths = await cloud_uploader.create_interest_folder_path_async(
                name=interest_name,
                dest=settings.CLOUD_PATH
            )

            if not cloud_paths:
                logger.error(f"{reg_id}: Не удалось создать папки для {interest_name}. Пропускаем интерес.")
                logger.info(
                    pipeline_event(
                        "interest_outcome",
                        reg_id=str(reg_id),
                        interest=str(interest_name),
                        outcome="fail_no_cloud",
                    )
                )
                self.del_pending_interest(reg_id, interest)
                return interest["end_time"]

            interest_cloud_folder = cloud_paths["interest_folder_path"]
            interest["cloud_folder"] = interest_cloud_folder
            pics_after_folder = posixpath.join(interest_cloud_folder, "after_pics")
            pics_before_folder = posixpath.join(interest_cloud_folder, "before_pics")
            interest["pics_before_folder"] = pics_before_folder
            interest["pics_after_folder"] = pics_after_folder
            await cloud_uploader.acreate_folder_if_not_exists(cloud_uploader.client, pics_before_folder)
            await cloud_uploader.acreate_folder_if_not_exists(cloud_uploader.client, pics_after_folder)

            # 1) проверяем наличие полного видео интереса в облаке
            interest_video_exists = await cloud_uploader.check_if_interest_video_exists(interest_name)

            # 2) какие каналы нужны для кадров
            before_channels_to_download, after_channels_to_download = await self.get_channels_to_download_pics(
                interest_cloud_folder
            )

            # 3) если видео по интересу в облаке НЕТ — добавляем канал полного ролика
            to_download_for_full_clip = [channel_id] if not interest_video_exists else []
            logger.debug(f"BEFORE,AFTER,FULL: {before_channels_to_download}, {after_channels_to_download}, {to_download_for_full_clip}")
            # детерминированное объединение без дублей
            final_channels_to_download = sorted({
                *before_channels_to_download,
                *after_channels_to_download,
                *to_download_for_full_clip
            })

            logger.debug(
                f"{reg_id}. {interest_name} Нужно скачать видео интереса: {not interest_video_exists}. "
                f"Кадры ДО: {before_channels_to_download}. "
                f"Кадры ПОСЛЕ: {after_channels_to_download}. "
                f"Итого каналы: {final_channels_to_download}"
            )

            if not final_channels_to_download:
                logger.info("Нечего скачивать, все материалы уже есть в облаке.")
                logger.info(
                    pipeline_event(
                        "interest_outcome",
                        reg_id=str(reg_id),
                        interest=str(interest_name),
                        outcome="skipped_already_in_cloud",
                    )
                )
                self.del_pending_interest(reg_id, interest)
                return None

            # 4) скачиваем по одному клипу на канал через cms_gate (ретраи HTTP в клиенте + раунды при дырках)
            def _all_requested_paths_present(m: dict) -> bool:
                for ch in final_channels_to_download:
                    info = m.get(ch) or m.get(str(ch))
                    if not (info or {}).get("path"):
                        return False
                return True

            download_rounds = max(1, settings.config.getint("Interests", "DOWNLOAD_INTEREST_ROUNDS", fallback=3))
            round_delay = float(
                settings.config.get("Interests", "DOWNLOAD_INTEREST_ROUND_DELAY_SEC", fallback="20")
            )
            max_attempts = settings.config.getint(
                "Interests", "INTEREST_MAX_DOWNLOAD_ATTEMPTS", fallback=5
            )
            channels_files_dict: dict = {}
            t_dl = time.perf_counter()

            # Перед каждым (включая первый) раундом сверяемся, что устройство
            # сейчас online. Это отсекает серии DeviceOfflineError-ретраев
            # внутри cms_gate_client (по 5 подряд), когда регистратор ушёл
            # оффлайн до или во время скачивания.
            async def _skip_if_offline(round_i: int) -> bool:
                try:
                    online = await cms_gate_client.is_device_online(reg_id)
                except Exception:
                    online = True  # fail-open
                if not online:
                    logger.info(
                        f"{reg_id}: {interest_name} раунд {round_i}/{download_rounds}: "
                        f"устройство offline — пропускаем раунд, интерес остаётся в pending."
                    )
                    logger.info(
                        pipeline_event(
                            "interest_download_skipped_offline",
                            reg_id=str(reg_id),
                            interest=str(interest_name),
                            round=round_i,
                            rounds_max=download_rounds,
                        )
                    )
                    return True
                return False

            try:
                offline_skipped = False
                for round_i in range(1, download_rounds + 1):
                    if await _skip_if_offline(round_i):
                        offline_skipped = True
                        break
                    channels_files_dict = await cms_gate_client.download_clips_for_interest(
                        reg_id=reg_id,
                        interest=interest,
                        channels=final_channels_to_download,
                    )
                    if _all_requested_paths_present(channels_files_dict):
                        break
                    if round_i < download_rounds:
                        missing = [
                            ch
                            for ch in final_channels_to_download
                            if not (channels_files_dict.get(ch) or channels_files_dict.get(str(ch)) or {}).get(
                                "path"
                            )
                        ]
                        logger.warning(
                            f"{reg_id}: {interest_name} раунд {round_i}/{download_rounds}: нет path по каналам "
                            f"{missing} — повтор через {round_delay * round_i:.0f}s"
                        )
                        await asyncio.sleep(min(120.0, round_delay * round_i))

                if offline_skipped:
                    # Интерес оставляем в pending, attempts НЕ инкрементим —
                    # это не ошибка обработки, а объективная недоступность DVR.
                    # Вернёмся к нему в следующем цикле, когда устройство
                    # снова окажется online.
                    return interest["end_time"]
            except Exception as exc:
                # Сеть/5xx/таймаут — учёт попыток. Исчерпали бюджет → dead-letter.
                attempts = main_funcs.inc_pending_download_attempts(reg_id, interest_name)
                logger.warning(
                    f"{reg_id}: download_clips_for_interest упал для {interest_name!r}: {exc!r}. "
                    f"download_attempts={attempts}/{max_attempts}"
                )
                if attempts >= max_attempts:
                    logger.error(
                        f"{reg_id}: dead-letter (out_of_attempts): {interest_name!r} "
                        f"исчерпал {max_attempts} попыток download-clips, удаляем из pending."
                    )
                    logger.info(
                        pipeline_event(
                            "interest_dead_letter",
                            reg_id=str(reg_id),
                            interest=str(interest_name),
                            reason="out_of_attempts",
                            attempts=attempts,
                            max_attempts=max_attempts,
                        )
                    )
                    self.del_pending_interest(reg_id, interest)
                logger.info(
                    pipeline_event(
                        "interest_outcome",
                        reg_id=str(reg_id),
                        interest=str(interest_name),
                        outcome="fail_download_clips",
                        attempts=attempts,
                        max_attempts=max_attempts,
                        pending_cleared=attempts >= max_attempts,
                    )
                )
                raise
            elapsed_dl = time.perf_counter() - t_dl
            missing_after = [
                ch
                for ch in final_channels_to_download
                if not (channels_files_dict.get(ch) or channels_files_dict.get(str(ch)) or {}).get("path")
            ]
            logger.info(
                pipeline_event(
                    "interest_download",
                    reg_id=str(reg_id),
                    interest=str(interest_name),
                    elapsed_sec=elapsed_dl,
                    rounds_max=download_rounds,
                    paths_complete=len(missing_after) == 0,
                    missing_count=len(missing_after),
                )
            )
            # оставляем полную структуру для доступа к concat_sources при отладке
            channels_info = channels_files_dict

            channels_paths = {ch: info["path"] for ch, info in channels_info.items() if info and info.get("path")}
            # 5) если надо — выгружаем «полный» клип в облако (только для chanel_id)
            full_clip_upload_status = False
            full_clip_path = None

            if not interest_video_exists:
                _ch_full = int(channel_id) if channel_id is not None else None
                file_dict = None
                if _ch_full is not None:
                    file_dict = channels_files_dict.get(_ch_full) or channels_files_dict.get(str(_ch_full))
                if file_dict:
                    full_clip_path = file_dict.get("path")
                else:
                    full_clip_path = None
                if full_clip_path:
                     full_clip_upload_status = await self.upload_interest_video_cloud(
                        reg_id=reg_id,
                        interest_name=interest_name,
                        video_path=full_clip_path,
                        cloud_folder=cloud_paths["interest_folder_path"]
                    )
                else:
                    logger.warning(
                        f"{reg_id}: Полный клип по каналу {channel_id} не получен — пропускаем загрузку видео.")

            await cloud_uploader.aupload_dict_as_json_to_cloud(
                data=interest["report"],
                remote_folder_path=interest["cloud_folder"]
            )

            # 6) извлекаем кадры из КАЖДОГО скачанного клипа и выгружаем их
            upload_status = await self.process_frames_before_after(
                reg_id, interest, channels_paths  # ← передаём словарь!!!
            )
            ok_frames = bool(upload_status and upload_status.get("upload_status"))
            logger.info(f"Результат загрузки изображений: {ok_frames}")

            # 7) чистим локальные клипы (кроме «полного» по нужному каналу)
            removed = video_utils.delete_videos_except(
                videos_by_channel=channels_paths,
                keep_channel_id=channel_id if not interest_video_exists else None,
            )
            need_full_video = not interest_video_exists
            video_ok = interest_video_exists or bool(full_clip_upload_status)
            all_done_ok = bool(ok_frames) and (not need_full_video or video_ok)

            if full_clip_path:
                if full_clip_upload_status:
                    logger.info(
                        f"{reg_id}: Удаляем локальное видео интереса {interest_name}. ({full_clip_path}).")
                    if os.path.exists(full_clip_path):
                        os.remove(full_clip_path)
                else:
                    logger.error(f"{reg_id}: Не удалось загрузить видео интереса в {interest_name}.")
            if all_done_ok:
                if settings.config.getboolean("QT_RM", "enable_recognition"):
                    logger.info(f"{reg_id}: {interest_name} Отдаем команду на распознавание (выстерлил-забыл)")
                    asyncio.create_task(
                        self.qt_rm_client.recognize_webdav(interest_name=interest_name)
                    )
                self.del_pending_interest(reg_id, interest)
                total_src_removed = 0
                for ch, info in channels_info.items():
                    sources = (info or {}).get("concat_sources") or []
                    for fp in sources:
                        try:
                            if os.path.exists(fp):
                                os.remove(fp)
                                total_src_removed += 1
                        except Exception as e:
                            logger.warning(f"{reg_id}: Не удалось удалить исходник {fp}: {e}")
                interest_temp_folder = os.path.join(settings.TEMP_FOLDER,
                                                    interest_name)
                if os.path.exists(interest_temp_folder):
                    logger.info(
                        f"{reg_id}: Удаляем временную директорию интереса {interest_name}. ({interest_temp_folder}).")
                    shutil.rmtree(interest_temp_folder)

            logger.info(f"{reg_id}: V2 завершено. Upload={upload_status}. Удалено видеофайлов: {removed}.")
            qt_sent = bool(
                all_done_ok
                and settings.config.getboolean("QT_RM", "enable_recognition")
            )
            logger.info(
                pipeline_event(
                    "interest_outcome",
                    reg_id=str(reg_id),
                    interest=str(interest_name),
                    outcome="success" if all_done_ok else "partial",
                    all_done_ok=all_done_ok,
                    video_ok=video_ok,
                    frames_ok=ok_frames,
                    pending_cleared=all_done_ok,
                    qt_recognition_scheduled=qt_sent,
                )
            )

        await cloud_uploader.append_report_line_to_cloud_async(
            remote_folder_path=cloud_paths["date_folder_path"],
            created_start_time=created_start_time.strftime(self.TIME_FMT),
            created_end_time=datetime.datetime.now().strftime(self.TIME_FMT),
            file_name=interest_name
        )

        # Маркируем интерес как обработанный (локально)
        #main_funcs._save_processed(reg_id, interest_name)

        return interest["end_time"]

    def del_pending_interest(self, reg_id, interest_or_name):
        names_to_remove = []
        if isinstance(interest_or_name, dict):
            merged_name = interest_or_name.get("name")
            if isinstance(merged_name, str) and merged_name:
                names_to_remove.append(merged_name)
            source_names = interest_or_name.get("_source_names") or []
            if isinstance(source_names, list):
                for nm in source_names:
                    if isinstance(nm, str) and nm:
                        names_to_remove.append(nm)
        elif isinstance(interest_or_name, str) and interest_or_name:
            names_to_remove.append(interest_or_name)

        # Стабильный порядок + дедуп
        unique_names = []
        seen = set()
        for nm in names_to_remove:
            if nm not in seen:
                unique_names.append(nm)
                seen.add(nm)

        if not unique_names:
            logger.warning(f"{reg_id}: del_pending_interest вызван без валидного имени интереса: {interest_or_name!r}")
            return

        for nm in unique_names:
            logger.info(f"{reg_id}: Удаляем pending interest: {nm}")
            try:
                main_funcs.remove_pending_interest(reg_id, nm)
            except Exception as e:
                logger.warning(f"{reg_id}: Не удалось удалить {nm} из pending_interests: {e}")


    async def get_channels_to_download_pics(self, interest_cloud_path):
        pics_after_folder = posixpath.join(interest_cloud_path, "after_pics")
        pics_before_folder = posixpath.join(interest_cloud_path, "before_pics")

        channels = [0, 1, 2, 3]

        # Параллельные проверки наличия на облаке
        before_checks = [asyncio.create_task(cloud_uploader._frame_exists_cloud_async(pics_before_folder, ch)) for ch in channels]
        after_checks = [asyncio.create_task(cloud_uploader._frame_exists_cloud_async(pics_after_folder, ch)) for ch in channels]

        before_exists = await asyncio.gather(*before_checks)
        after_exists = await asyncio.gather(*after_checks)
        before_channels_to_download = [ch for ch, exists in zip(channels, before_exists) if not exists]
        after_channels_to_download = [ch for ch, exists in zip(channels, after_exists) if not exists]
        return before_channels_to_download, after_channels_to_download

    async def process_frames_before_after(self, reg_id: str, enriched: dict, videos_by_channel):
        """
        ВЕРСИЯ 3 (streaming):
        1) Из каждого клипа берём первый и последний кадр как JPEG bytes (без локальных файлов)
        2) Заливаем в облако в before_pics / after_pics через PUT
        Возвращает: {"upload_status": bool}
        """
        channels = [0, 1, 2, 3]

        before_items: list[tuple[str, bytes]] = []
        after_items: list[tuple[str, bytes]] = []

        async def _extract_for_channel(ch: int, path: str | None):
            if not path:
                return None, None
            return await video_utils.extract_edge_frames_bytes(
                video_path=path,
                channel_id=ch,
                reg_id=reg_id,
            )

        # Создаём словарь task -> channel для корректного маппинга
        task_to_channel = {}
        for ch in channels:
            task = asyncio.create_task(_extract_for_channel(ch, videos_by_channel.get(ch)))
            task_to_channel[task] = ch

        # Собираем результаты по мере готовности
        for task in asyncio.as_completed(task_to_channel.keys()):
            first_item, last_item = await task
            if first_item:
                before_items.append(first_item)
            if last_item:
                after_items.append(last_item)

        # Загрузка без временных файлов
        ok_before = await cloud_uploader.upload_many_bytes_async(before_items, enriched["pics_before_folder"],
                                                                 content_type="image/jpeg")
        ok_after = await cloud_uploader.upload_many_bytes_async(after_items, enriched["pics_after_folder"],
                                                                content_type="image/jpeg")
        upload_status = bool(ok_before and ok_after)

        return {"upload_status": upload_status}


    async def upload_interest_video_cloud(self, reg_id, interest_name, video_path, cloud_folder):
        # Загружаем видео
        logger.info(
            f"{reg_id}: Загружаем видео интереса {interest_name} в облако.")
        upload_status = await asyncio.to_thread(
            cloud_uploader.upload_file, video_path, cloud_folder)
        return upload_status

    async def login(self):
        """
        Авторизация в CMS больше не требуется напрямую, так как все обращения
        к CMS идут через внешний сервис cms_gate.
        """
        self.jsession = None

    async def _gc_gaps_if_due(self) -> None:
        """
        Этап 4: раз в GC_INTERVAL_SEC (по умолчанию 3600с) чистит терминальные
        gap'ы (closed/abandoned) старше TTL по всем регистраторам.
        Троттлится через `self._last_gc_gaps_ts`.
        """
        gc_interval_sec = 3600
        now_ts = time.time()
        last = getattr(self, "_last_gc_gaps_ts", 0.0) or 0.0
        if (now_ts - last) < gc_interval_sec:
            return
        self._last_gc_gaps_ts = now_ts
        ttl_closed = settings.config.getint("Interests", "GAP_TTL_DAYS_CLOSED", fallback=7)
        ttl_abnd = settings.config.getint("Interests", "GAP_TTL_DAYS_ABANDONED", fallback=30)
        try:
            await asyncio.to_thread(
                main_funcs.gc_gaps_all,
                ttl_closed_days=ttl_closed,
                ttl_abandoned_days=ttl_abnd,
            )
        except Exception:
            logger.exception("[gc_gaps_all] failed")

    async def mainloop(self):
        logger.info("Mainloop has been launched with success.")
        self._running: set[asyncio.Task] = set()
        self._last_gc_gaps_ts = 0.0
        await self.login()

        try:
            await sync_trucks_from_gate()
        except Exception as e:
            logger.exception("[vehicle_sync] initial sync_trucks_from_gate failed: %s", e)
        interval = trucks_sync_interval_sec()
        asyncio.create_task(run_periodic_trucks_sync_after_delay(interval))

        while True:
            try:
                await self._gc_gaps_if_due()
                # важно: get_devices_online в thread, чтобы не блокировать loop
                devices_online = await self.get_devices_online()

                for device_dict in devices_online:
                    reg_id = device_dict["did"]
                    plate = device_dict["vid"]

                    # если девайс уже в работе — пропускаем
                    if reg_id in self.devices_in_progress:
                        continue

                    async def _run_with_limit(rid, pl):
                        async with self._get_devices_sem():
                            await self.operate_device(rid, pl)

                    # Стартуем корутину и НЕ ждём всю пачку
                    t = asyncio.create_task(_run_with_limit(reg_id, plate))
                    self._running.add(t)
                    t.add_done_callback(self._running.discard)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Защитный барьер: любая неожиданная ошибка в итерации
                # не должна убивать mainloop. Логируем и идём на следующий тик.
                logger.exception(f"mainloop iteration failed: {e}")

            await asyncio.sleep(120)

    async def _refill_pending_interests_if_due(self, reg_id: str) -> None:
        """
        Пополняет очередь pending_interests для reg_id forward-проходом:

           - если сейчас > last_upload_time + 600 сек:
             - догоняем интервал [last_upload_time → now] посуточно,
               обновляя last_upload_time по мере продвижения.

        Ограничение глубины по дням (MAX_LOOKBACK_DAYS) применяется к last_upload_time,
        чтобы не уходить слишком далеко в прошлое.
        """
        if reg_id in self._interest_refill_in_progress:
            return
        self._interest_refill_in_progress.add(reg_id)
        try:
            TIME_FMT = "%Y-%m-%d %H:%M:%S"

            reg_info = main_funcs.get_reg_info(reg_id)

            last_up_str = reg_info.get("last_upload_time")
            if not last_up_str:
                last_up_str = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime(TIME_FMT)
            last_up = datetime.datetime.strptime(last_up_str, TIME_FMT)

            now = datetime.datetime.now()

            forward_due = (now - last_up).total_seconds() >= 600
            if not forward_due:
                return

            max_lookback_days = settings.config.getint("Interests", "MAX_LOOKBACK_DAYS", fallback=0)
            if max_lookback_days > 0:
                earliest_allowed = now - datetime.timedelta(days=max_lookback_days)
                if last_up < earliest_allowed:
                    logger.info(
                        f"{reg_id}: last_upload_time={last_up.strftime(TIME_FMT)} старее окна "
                        f"{max_lookback_days}d → берём не глубже {earliest_allowed.strftime(TIME_FMT)}."
                    )
                    last_up = earliest_allowed

            collected: list[dict] = []

            def day_end(dt: datetime.datetime) -> datetime.datetime:
                return dt.replace(hour=23, minute=59, second=59)

            cur = last_up
            today = now.date()

            while cur.date() < today:
                st = cur.strftime(TIME_FMT)
                en_dt = day_end(cur)
                en = en_dt.strftime(TIME_FMT)

                reg_cfg = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate=None)
                interests = await self.get_interests_async(reg_id, reg_cfg, st, en)
                if interests:
                    self._log_interests_shape(reg_id, "FORWARD_DAY_BEFORE_MERGE", interests)
                    merged = self._merge_interests_safe(reg_id, "FORWARD_DAY_BEFORE_MERGE", interests)
                    if merged is None:
                        return
                    if merged:
                        collected.extend(merged)
                        en = max(interest["end_time"] for interest in merged)

                main_funcs.save_new_reg_last_upload_time(reg_id, en)
                cur = en_dt + datetime.timedelta(seconds=1)

            if cur <= now:
                st = cur.strftime(TIME_FMT)
                en = now.strftime(TIME_FMT)
                reg_cfg = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate=None)
                interests = await self.get_interests_async(reg_id, reg_cfg, st, en)
                if interests:
                    self._log_interests_shape(reg_id, "FORWARD_TODAY_BEFORE_MERGE", interests)
                    merged = self._merge_interests_safe(reg_id, "FORWARD_TODAY_BEFORE_MERGE", interests)
                    if merged is None:
                        return
                    if merged:
                        collected.extend(merged)
                        en = max(interest["end_time"] for interest in merged)
                main_funcs.save_new_reg_last_upload_time(reg_id, en)

            if collected:
                main_funcs.append_pending_interests(reg_id, collected)

            try:
                self._recompute_gap_links(reg_id)
            except Exception:
                logger.exception(f"{reg_id}: _recompute_gap_links failed")

            try:
                await self._process_gaps_if_due(reg_id)
            except Exception:
                logger.exception(f"{reg_id}: _process_gaps_if_due failed")

        finally:
            self._interest_refill_in_progress.discard(reg_id)



async def _run():
    d = Main()
    try:
        await d.mainloop()
    finally:
        # всегда освобождаем соединения cms_gate_client
        await cms_gate_client.close_client()


if __name__ == "__main__":
    asyncio.run(_run())
