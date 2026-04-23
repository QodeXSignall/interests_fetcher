from typing import Optional, Dict, Any, List, Tuple
from interests_fetcher.functions import get_reg_info
from interests_fetcher import geo_funcs
from interests_fetcher.logger import logger
from interests_fetcher.data import settings
from bisect import bisect_left
import datetime
import functools
import asyncio
import httpx


io_to_reg_map = {1: 20, 2: 21, 3: 22, 4: 23}


class LoadingInProgress(RuntimeError):
    """ Погрузка еще в процессе, прерываем поиск интересов """
    pass

def get_interest_from_track(track, start_time: str, end_time: str,
                            photo_before_timestamp: str = None,
                            photo_after_timestamp: str = None,
                            reg_id = None):
    start_time_datetime = datetime.datetime.strptime(start_time,
                                                     "%Y-%m-%d %H:%M:%S")
    end_time_datetime = datetime.datetime.strptime(end_time,
                                                   "%Y-%m-%d %H:%M:%S")
    photo_before_datetime = datetime.datetime.strptime(photo_before_timestamp,
                                                       "%Y-%m-%d %H:%M:%S")
    photo_after_datetime = datetime.datetime.strptime(photo_after_timestamp,
                                                      "%Y-%m-%d %H:%M:%S")
    return {
        "name": f"{track['vid']}_"
                f"{start_time_datetime.year}."
                f"{start_time_datetime.month:02d}."
                f"{start_time_datetime.day:02d} "
                f"{start_time_datetime.hour:02d}."
                f"{start_time_datetime.minute:02d}."
                f"{start_time_datetime.second:02d}-"
                f"{end_time_datetime.hour:02d}."
                f"{end_time_datetime.minute:02d}."
                f"{end_time_datetime.second:02d}",
        "reg_id": reg_id,
        "beg_sec": seconds_since_midnight(start_time_datetime),
        "end_sec": seconds_since_midnight(end_time_datetime),
        "year": start_time_datetime.year,
        "month": start_time_datetime.month,
        "day": start_time_datetime.day,
        "start_time": start_time,
        "end_time": end_time,
        "car_number": track["vid"],
        "photo_before_timestamp": photo_before_timestamp,
        "photo_after_timestamp": photo_after_timestamp,
        "photo_before_sec": seconds_since_midnight(photo_before_datetime),
        "photo_after_sec": seconds_since_midnight(photo_after_datetime),
    }


def _safe_fromtimestamp_sec(ts_sec: float) -> datetime:
    # на входе секунды (float/int)
    dt_utc = datetime.datetime.fromtimestamp(ts_sec, tz=datetime.timezone.utc)
    return dt_utc.astimezone().replace(tzinfo=None)


def _parse_alarm_time(a: Dict[str, Any], key_ms: str, key_str: str) -> Tuple[datetime.datetime | None, str | None]:
    """
    Пытается достать время из ms-поля (UTC ms), если нет — из строкового поля '%Y-%m-%d %H:%M:%S'.
    Возвращает (dt_local_naive, formatted_str) или (None, None).
    """
    ms = a.get(key_ms)
    if isinstance(ms, (int, float)):
        try:
            dt_local = _safe_fromtimestamp_sec(ms / 1000.0)
            return dt_local, dt_local.strftime(settings.TIME_FMT)
        except Exception:
            pass

    s = a.get(key_str)
    if isinstance(s, str) and s.strip():
        try:
            # строки обычно уже в локальном формате без TZ
            dt_local = datetime.datetime.strptime(s.strip(), settings.TIME_FMT)
            return dt_local, s.strip()
        except Exception:
            pass

    return None, None

def _atp_to_io_index(atp_str: Optional[str]) -> Optional[int]:
    # Пример: atp=22, atpStr='IO_4报警' → вернём 4
    if atp_str and "IO_" in atp_str:
        try:
            return int(atp_str.split("IO_")[1].split("报警")[0])
        except Exception:
            pass
    # если строка не пришла — можно маппить по числам, если знаешь соответствие
    return None

def _resolve_cluster_cargo(types: set[str]) -> str:
    # приоритет: kgo > euro > unknown
    if "kgo" in types:
        return "kgo"
    if "euro" in types:
        return "euro"
    return "unknown"

def _dedupe_sw(events: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for e in events:
        key = (e.get("datetime"), e.get("switch"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out

def _cluster_merge_stationary(norm: list[dict], min_stop_kmh: float, merge_gap_sec: int) -> list[dict]:
    """
    Сливает подряд идущие алармы ОДНОГО IO в один кластер, если:
      - один и тот же девайс
      - один и тот же io_index
      - стояли в момент старта (ssp_kmh <= min_stop_kmh)
      - промежуток между алармами не больше merge_gap_sec

    Возвращает список кластеров; каждый кластер — как "alarms"-запись, но с полем
    switch_events / io_indices_set.
    """
    if not norm:
        return []

    clusters: list[dict] = []
    cur = None

    for a in norm:
        # гарантируем наличие служебных полей
        a.setdefault("switch_events", [
            {"datetime": a["start_str"], "switch": a.get("io_index")}
        ])
        a.setdefault(
            "io_indices_set",
            set([a.get("io_index")] if a.get("io_index") is not None else [])
        )

        a_stopped = a.get("ssp_kmh", 999) <= min_stop_kmh

        if cur is None:
            cur = a.copy()
            cur["io_indices_set"] = set(cur.get("io_indices_set", set()))
            continue

        same_dev = (cur.get("dev_idno") == a.get("dev_idno"))
        same_io  = (cur.get("io_index") == a.get("io_index"))
        gap_ok   = (a["start_ts"] - cur["end_ts"] <= merge_gap_sec)

        if same_dev and same_io and a_stopped and gap_ok:
            # сливаем в текущий кластер
            cur["end_dt"]  = max(cur["end_dt"], a["end_dt"])
            cur["end_ts"]  = max(cur["end_ts"], a["end_ts"])
            cur["end_str"] = cur["end_dt"].strftime(settings.TIME_FMT)
            cur["esp_kmh"] = a.get("esp_kmh", cur.get("esp_kmh"))

            # объединяем типы груза
            cur_types = set([cur.get("cargo_type", "unknown")])
            cur_types |= set([a.get("cargo_type", "unknown")])
            cur["cargo_type"] = _resolve_cluster_cargo({t for t in cur_types if t})

            # io_indices_set по идее будет из одного индекса, но оставляем как есть
            cur["io_indices_set"] |= a.get("io_indices_set", set())

            # объединяем switch_events с дедупликацией
            cur["switch_events"] = _dedupe_sw(
                (cur.get("switch_events") or []) + (a.get("switch_events") or [])
            )
        else:
            # завершаем предыдущий кластер и начинаем новый
            clusters.append(cur)
            cur = a.copy()
            cur["io_indices_set"] = set(cur.get("io_indices_set", set()))

    if cur is not None:
        clusters.append(cur)

    return clusters


def _merge_or_append(loading_intervals, new_interval, epsilon_sec=30):
    """Если в списке уже есть интерес с тем же стартом (±epsilon) и тем же cargo_type — расширяем его конец и события."""
    from datetime import datetime
    fmt = "%Y-%m-%d %H:%M:%S"

    new_start = datetime.strptime(new_interval["start_time"], fmt)
    new_end   = datetime.strptime(new_interval["end_time"], fmt)
    new_cargo = new_interval.get("report", {}).get("cargo_type")

    for cur in loading_intervals:
        cur_cargo = cur.get("report", {}).get("cargo_type")
        if cur_cargo != new_cargo:
            continue
        cur_start = datetime.strptime(cur["start_time"], fmt)

        if abs((cur_start - new_start).total_seconds()) <= epsilon_sec:
            # расширяем конец, если новый длиннее
            cur_end = datetime.strptime(cur["end_time"], fmt)
            if new_end > cur_end:
                cur["end_time"] = new_interval["end_time"]
                cur["photo_after_timestamp"] = new_interval.get("photo_after_timestamp", cur.get("photo_after_timestamp"))

            # мерджим события переключателей
            cur_sw = cur.get("report", {}).get("switch_events", [])
            new_sw = new_interval.get("report", {}).get("switch_events", [])
            # простая дедупликация по (datetime, switch, source)
            seen = { (e.get("datetime"), e.get("switch"), e.get("source")) for e in cur_sw }
            for e in new_sw:
                key = (e.get("datetime"), e.get("switch"), e.get("source"))
                if key not in seen:
                    cur_sw.append(e); seen.add(key)
            cur["report"]["switch_events"] = cur_sw
            return True  # поглотили

    # похожего не нашли — добавляем как новый
    loading_intervals.append(new_interval)
    return False

def prepare_alarms(raw_alarms: List[Dict[str, Any]],
                   reg_cfg: Dict[str, Any],
                   allowed_atp: set[int] = frozenset({19,20,21,22}),
                   min_stop_speed_kmh: float = 5.0,
                   merge_gap_sec: int = 15,
                   reg_id = None) -> Dict[str, Any]:
    """
    Превращает raw alarms из API в удобный список для поиска «в разрывах».
    Возвращает:
      {
        "alarms": [ ... нормализованные ... ],
        "starts": [ start_ts ... ]  # для bisect
      }
    """
    euro_alarm = int(reg_cfg.get("euro_container_alarm", 4)) if reg_cfg else None
    kgo_alarm_val = reg_cfg.get("kgo_container_alarm") if reg_cfg else None
    kgo_alarm = int(kgo_alarm_val) if kgo_alarm_val is not None else None

    # 1) фильтр нужных типов
    filtered = [a for a in raw_alarms if (a.get("atp") in allowed_atp)]

    # 2) нормализация
    norm: List[Dict[str, Any]] = []
    for a in filtered:

        start_dt, start_str = _parse_alarm_time(a, "stm", "bTimeStr")
        if not start_dt:
            try:
                logger.warning(f"[ALARMS] Пропуск записи без stm: guid={a.get('guid')}")
            except NameError:
                pass
            continue

        # Время конца (если нет etm → fallback от stm)
        end_dt, end_str = _parse_alarm_time(a, "etm", "eTimeStr")
        if not end_dt:
            fb_after = 30  # можно взять из settings.config.getint("Interests", "AFTER_FALLBACK_SEC", fallback=30)
            end_dt = start_dt + datetime.timedelta(seconds=fb_after)
            end_str = end_dt.strftime(settings.TIME_FMT)
        ssp_kmh = (a.get("ssp") or 0) / 10.0
        esp_kmh = (a.get("esp") or 0) / 10.0

        io_idx = _atp_to_io_index(a.get("atpStr") or "")
        if io_idx == euro_alarm:
            cargo = "euro"
        elif kgo_alarm is not None and io_idx == kgo_alarm:
            cargo = "kgo"
        else:
            cargo = "unknown"

        norm.append({
            "guid": a.get("guid"),
            "dev_idno": reg_id,
            "plate": a.get("vid"),
            "atp": a.get("atp"),
            "atpStr": a.get("atpStr"),
            "io_index": io_idx,
            "cargo_type": cargo,

            "start_dt": start_dt,
            "end_dt": end_dt,
            "start_ts": start_dt.timestamp(),
            "end_ts": end_dt.timestamp(),
            "start_str": start_str,
            "end_str": end_str,

            "ssp_kmh": ssp_kmh,
            "esp_kmh": esp_kmh,
            "start_stopped": (ssp_kmh <= min_stop_speed_kmh),

            # координаты и пр. можно оставить при необходимости:
            "slng": a.get("slng"), "slat": a.get("slat"),
            "elng": a.get("elng"), "elat": a.get("elat"),
            "smlng": a.get("smlng"), "smlat": a.get("smlat"),
            "emlng": a.get("emlng"), "emlat": a.get("emlat"),
        })

    # 3) сортировка
    norm.sort(key=lambda x: x["start_ts"])

    """
    ## 4) (опционально) объединить «дрожание» одного IO в один блок
    merged: List[Dict[str, Any]] = []
    for a in norm:
        if not merged:
            merged.append(a); continue
        prev = merged[-1]
        same_io = (a["io_index"] == prev["io_index"]) and (a["cargo_type"] == prev["cargo_type"])
        close_enough = (a["start_ts"] - prev["end_ts"] <= merge_gap_sec)
        if same_io and close_enough:
            # расширяем конец и переносим esp/флаги по необходимости
            prev["end_dt"] = a["end_dt"]
            prev["end_ts"] = a["end_ts"]
            prev["end_str"] = a["end_str"]
            prev["esp_kmh"] = a["esp_kmh"]
        else:
            merged.append(a)

    starts = [a["start_ts"] for a in norm]
    """
    merged = _cluster_merge_stationary(norm, min_stop_speed_kmh, merge_gap_sec)

    starts = [a["start_ts"] for a in merged]
    return {"alarms": merged, "starts": starts}


def estimate_move_start_kmhps(
    t0,                 # datetime: последний наблюдаемый «стоп»-трек
    t1,                 # datetime: первый «едем»-трек
    v1_kmh,             # скорость на t1 (в тех же единицах, что MIN_MOVE_SPEED)
    min_move_speed,     # порог «едем» (км/ч)
    small_gap_sec=5,    # «малый разрыв»
    max_gap_sec=30,     # «большой разрыв»
    A_KMHPS=1.26,       # типовое ускорение (км/ч за секунду), 1.26 ~= 0.35 м/с²
    clamp_eps=0.1       # небольшой зазор от границ окна
):
    """
    Возвращает оценку момента старта движения (datetime) в интервале (t0, t1).
    Всё в километрах/час и секундах, без перевода в м/с.
    """
    dt = (t1 - t0).total_seconds()

    # 1) Малый разрыв — консервативно ближе к t1
    if dt <= small_gap_sec:
        t_move = t1 - datetime.timedelta(seconds=min(1.0, dt / 2.0))
    else:
        # 2) Оценка по «физике» разгона в км/ч/сек
        A = max(A_KMHPS, 1e-6)
        tau_sec = max(0.0, v1_kmh) / A
        t_move = t1 - datetime.timedelta(seconds=tau_sec)

    # 3) Очень большая дырка — смягчим, если еле тронулись
    if dt > max_gap_sec and v1_kmh <= min_move_speed * 2:
        t_move = max(t1 - datetime.timedelta(seconds=1.0), t0 + datetime.timedelta(seconds=clamp_eps))

    # 4) Зажать в рамки окна
    lo = t0 + datetime.timedelta(seconds=clamp_eps)
    hi = t1 - datetime.timedelta(seconds=clamp_eps)
    if t_move < lo: t_move = lo
    if t_move > hi: t_move = hi

    return t_move

def estimate_stop_start_kmhps(
    t_move,             # datetime: последний «движущийся» трек до возвращения в стоп
    t_stop,             # datetime: первый «стоп»-трек (v <= min_stop_speed)
    v0_kmh,             # скорость на t_move (км/ч)
    min_stop_speed,     # порог «стоп» (км/ч)
    small_gap_sec=5,    # «малый разрыв»
    max_gap_sec=30,     # «большой разрыв»
    D_KMHPS=1.8,        # типичное замедление (км/ч за секунду), ≈0.5 м/с²
    clamp_eps=0.1       # зазор от границ окна
):
    """
    Оценивает момент начала остановки в интервале (t_move, t_stop].
    Все величины в км/ч и секундах (без перевода в м/с).
    """
    dt = (t_stop - t_move).total_seconds()

    if dt <= small_gap_sec:
        # при малом разрыве — ближе к t_stop, но не ровно t_stop
        t_est = t_stop - datetime.timedelta(seconds=min(1.0, max(dt * 0.5, clamp_eps)))
    else:
        # оценка по торможению: время до достижения порога стопа
        D = max(D_KMHPS, 1e-6)
        v_over = max(float(v0_kmh) - float(min_stop_speed), 0.0)
        tau_sec = v_over / D
        t_est = t_move + datetime.timedelta(seconds=tau_sec)

    # подправим при больших дырках и низкой скорости
    if dt > max_gap_sec and v0_kmh <= max(min_stop_speed * 2, 5):
        t_est = max(t_stop - datetime.timedelta(seconds=1.0), t_move + datetime.timedelta(seconds=clamp_eps))

    # зажать внутри (t_move+eps, t_stop-eps)
    lo = t_move + datetime.timedelta(seconds=clamp_eps)
    hi = t_stop - datetime.timedelta(seconds=clamp_eps)
    if t_est < lo: t_est = lo
    if t_est > hi: t_est = hi

    t_est = t_est.strftime(settings.TIME_FMT)
    t_est = t_est.strftime(settings.TIME_FMT)
    return t_est


def find_interests_by_lifting_switches(
        tracks, sec_before=30, sec_after=60, start_tracks_search_time=None, reg_id=None, alarms=None):
    """
    tracks – список треков CMS (gt, s1, sp, ps и т.д.)
    alarms – ПОДГОТОВЛЕННЫЕ алармы: {"alarms": [...], "starts": [...]}, см. prepare_alarms(...)
             Если формат иной или None — логика по алармам будет пропущена (ничего не ломаем).
    """
    loading_intervals = []
    i = 0
    first_interest = True   # Используем в случаях, когда для первого интереса не найдена начальная остановка в заданных треках
    reg_cfg = get_reg_info(reg_id) if reg_id else None

    try:
        ignore_points = geo_funcs.get_ignore_points()
    except Exception as e:
        logger.warning(f"{reg_id}: [IGNORE] Не удалось загрузить ignore_points: {e}")
        ignore_points = []
    ignore_tolerance = settings.config.getint("Interests", "IGNORE_POINTS_TOLERANCE", fallback=0)

    def _ignored_geo(geo: str | None) -> str | None:
        if not geo or not ignore_points or ignore_tolerance <= 0:
            return None
        try:
            return geo_funcs.find_nearby_name(geo, ignore_points, ignore_tolerance)
        except Exception as e:
            logger.warning(f"{reg_id}: [IGNORE] Ошибка проверки точки игнора: {e}")
            return None

    try:
        euro_alarm_cfg = int((reg_cfg or {}).get("euro_container_alarm", 4))
    except Exception:
        euro_alarm_cfg = 4
    try:
        kgo_alarm_cfg_val = (reg_cfg or {}).get("kgo_container_alarm")
        kgo_alarm_cfg = int(kgo_alarm_cfg_val) if kgo_alarm_cfg_val is not None else None
    except Exception:
        kgo_alarm_cfg = None

    euro_bit_idx = io_to_reg_map.get(euro_alarm_cfg, 23)
    kgo_bit_idx = io_to_reg_map.get(kgo_alarm_cfg, None) if kgo_alarm_cfg is not None else None

    last_stop_started_at: datetime.datetime | None = None
    prev_speed: int | None = None
    min_stop_speed = settings.config.getint("Interests", "MIN_STOP_SPEED")
    min_move_speed = settings.config.getint("Interests", "MIN_MOVE_SPEED")
    min_stop_duration = settings.config.getint("Interests", "MIN_STOP_DURATION_SEC")
    def _update_stop_state(cur_dt: datetime.datetime, cur_speed: int):
        nonlocal last_stop_started_at, prev_speed
        # старт остановки при переходе через порог вниз
        if cur_speed <= min_stop_speed and (prev_speed is None or prev_speed > min_stop_speed):
            last_stop_started_at = cur_dt
        # любая устойчивая «поехали» сбрасывает маркер остановки
        if cur_speed >= min_move_speed:
            last_stop_started_at = None
        prev_speed = cur_speed

    # --- локальная утилита для быстрого поиска алармов в окне разрыва ---
    def _alarms_in_gap(prepared, gap_start_ts, gap_end_ts):
        """prepared == {"alarms": [...], "starts": [...]}"""
        try:
            from bisect import bisect_left
            _alarms = prepared.get("alarms") or []
            _starts = prepared.get("starts") or []
            idx = bisect_left(_starts, gap_start_ts)
            out = []
            while idx < len(_alarms) and _alarms[idx].get("start_ts", 0) <= gap_end_ts:
                a = _alarms[idx]
                if a.get("end_ts", 0) >= gap_start_ts:
                    out.append(a)
                idx += 1
            return out
        except Exception as e:
            logger.warning(f"{reg_id}: [ALARM GAP] Не удалось выбрать алармы в разрыве: {e}")
            return []

    while i < len(tracks) - 1:
        # Защита от выхода за границы для next_track
        if i + 1 >= len(tracks):
            break
        track = tracks[i]
        next_track = tracks[i + 1]
        cur_speed = int(track.get("sp") or 0)
        t_curr_dt = track.get("gt")  # строка (для логов)
        t_next_dt = next_track.get("gt")  # строка (для логов)

        # --- вычисление разрыва между текущим треком и следующим ---
        t_curr = datetime.datetime.strptime(t_curr_dt, "%Y-%m-%d %H:%M:%S")
        t_next = datetime.datetime.strptime(t_next_dt, "%Y-%m-%d %H:%M:%S")

        _update_stop_state(t_curr, cur_speed)

        gap_sec = (t_next - t_curr).total_seconds()
        GAP_THRESHOLD = settings.config.getint("Interests", "GAP_THRESHOLD_SEC", fallback=10)
        if gap_sec > GAP_THRESHOLD:
            logger.debug(f"{reg_id}: Обнаружен разрыв в треках: {t_curr} → {t_next} = {gap_sec:.1f}s")

        # === Новая вставка: обработка "разрыва" через алармы (если они переданы и подготовлены) ===
        if alarms and isinstance(alarms, dict) and "alarms" in alarms and "starts" in alarms and gap_sec > GAP_THRESHOLD:
            gap_start_ts = t_curr.timestamp()
            gap_end_ts = t_next.timestamp()
            gap_alarms = _alarms_in_gap(alarms, gap_start_ts, gap_end_ts)

            if gap_alarms:
                logger.debug(f"{reg_id}:[ALARM GAP] Разрыв {t_curr_dt} → {t_next_dt} ({int(gap_sec)}s), найдено алармов: {len(gap_alarms)}")

            for a in gap_alarms:
                alarm_dt = a.get("start_dt")
                alarm_ts_str = a.get("start_str")
                #gap_start_dt = datetime.datetime.strptime(gap_start_ts, "%Y-%m-%d %H:%M:%S")

                logger.info(f"{reg_id}: Обработка аларма в {alarm_ts_str}")

                # Принимаем только «стоял в начале события» и известный тип груза
                start_stopped = a.get("start_stopped")

                if not start_stopped:
                    logger.debug(f"{reg_id}: [GAP ALARM] Not start stopped, continue... ")
                    continue
                logger.debug(a)
                cargo_key = a.get("cargo_type", "unknown")
                io_index = a.get("io_index")
                atp_str = a.get("atpStr", "")
                
                # Fallback: если тип unknown, но есть аларм в разрыве и start_stopped=True,
                # попробуем определить тип по io_index или использовать дефолтный тип
                if cargo_key == "unknown":
                    logger.warning(
                        f"{reg_id}: [GAP ALARM] Unknown cargo type: io_index={io_index}, atpStr='{atp_str}', "
                        f"euro_alarm_cfg={euro_alarm_cfg}, kgo_alarm_cfg={kgo_alarm_cfg}"
                    )
                    # Попробуем определить тип по io_index, если он совпадает с одним из известных значений
                    if io_index is not None:
                        if io_index == euro_alarm_cfg:
                            cargo_key = "euro"
                            logger.info(f"{reg_id}: [GAP ALARM] Определен тип 'euro' по io_index={io_index}")
                        elif kgo_alarm_cfg is not None and io_index == kgo_alarm_cfg:
                            cargo_key = "kgo"
                            logger.info(f"{reg_id}: [GAP ALARM] Определен тип 'kgo' по io_index={io_index}")
                        else:
                            # Если не совпадает, используем дефолтный тип "euro" как наиболее распространенный
                            # но только если это действительно аларм подъема (atp 19-22)
                            atp = a.get("atp")
                            if atp in (19, 20, 21, 22):
                                cargo_key = "euro"  # Дефолтный тип для алармов подъема
                                logger.warning(
                                    f"{reg_id}: [GAP ALARM] Используем дефолтный тип 'euro' для аларма "
                                    f"io_index={io_index}, atp={atp}, atpStr='{atp_str}'"
                                )
                            else:
                                logger.warning(f"{reg_id}: [GAP ALARM] Пропускаем аларм с неизвестным типом груза")
                                continue
                    else:
                        logger.warning(f"{reg_id}: [GAP ALARM] Пропускаем аларм: io_index=None")
                        continue
                
                cargo_type_alarm = "Бункер" if cargo_key == "kgo" else "Контейнер"

                delta_last_track_to_alarm_seconds =  (alarm_dt - t_curr).total_seconds()
                delta_alarm_to_first_track_seconds = (t_next - alarm_dt).total_seconds()
                logger.debug(f"{reg_id}: [ALARM GAP] delta_last_track_to_alarm_seconds - {delta_last_track_to_alarm_seconds}")
                if delta_last_track_to_alarm_seconds > 30:
                    logger.warning(
                        f"{reg_id}: [GAP] Разрыв {delta_last_track_to_alarm_seconds:.1f}с до аларма {alarm_dt}. "
                        f"Оцениваем время ДО внутри разрыва."
                    )
                    # оценим «последнюю секунду устойчивой остановки» внутри окна (t_curr..alarm_dt)
                    # кейс 1: мы и так стояли на последнем треке — берём почти у самого аларма
                    min_stop_speed = settings.config.getint("Interests", "MIN_STOP_SPEED")
                    v_prev = int(track.get("sp") or 0)
                    eps = 0.1
                    lo = t_curr + datetime.timedelta(seconds=eps)
                    hi = alarm_dt - datetime.timedelta(seconds=eps)


                    if v_prev <= min_stop_speed or a.get("start_stopped"):
                        #t_before_dt = max(lo, alarm_dt - datetime.timedelta(seconds=1))
                        #time_before = t_before_dt.strftime("%Y-%m-%d %H:%M:%S")
                        #logger.debug(f"Кейс 1. Оцененный time_before: {time_before}")
                        time_before = alarm_dt - datetime.timedelta(seconds=30)
                        time_before = time_before.strftime(settings.TIME_FMT)
                    else:
                        # кейс 2: на последнем треке мы НЕ стояли → пробуем обычный поиск по времени (он уже устойчив к дыркам)
                        time_before = find_first_stable_stop(
                            tracks, i, alarm_dt, settings, first_interest, start_tracks_search_time, reg_id
                        )
                        logger.debug(f"Кейс 2. Оцененный time_before: {time_before}")
                else:
                    time_before = find_first_stable_stop(tracks, i, alarm_dt, settings, first_interest,
                                                         start_tracks_search_time, reg_id)
                if not time_before:
                    # Для короткого окна (например, i=0) подтвержденная остановка может не собраться.
                    # В этом случае используем безопасный fallback рядом с alarm_dt вместо падения.
                    short_window_fallback_sec = settings.config.getint(
                        "Interests", "SHORT_WINDOW_FALLBACK_SEC", fallback=30
                    )
                    if delta_last_track_to_alarm_seconds > 0:
                        fallback_anchor = max(t_curr, alarm_dt - datetime.timedelta(seconds=short_window_fallback_sec))
                    else:
                        fallback_anchor = alarm_dt - datetime.timedelta(seconds=short_window_fallback_sec)
                    time_before = fallback_anchor.strftime(settings.TIME_FMT)
                    logger.warning(
                        f"{reg_id}: [TIME_BEFORE-FALLBACK] Короткое/шумное окно перед alarm, "
                        f"используем оценку {time_before} (i={i}, delta={delta_last_track_to_alarm_seconds:.1f}s)"
                    )

                if delta_alarm_to_first_track_seconds > 30:
                    logger.warning(
                        f"{reg_id}:[AFTER] Пропуск поиска движения — первый трек после разрыва ({t_next}) опережает момент срабатывания ({alarm_dt}) на {delta_alarm_to_first_track_seconds:.1f}с. Используем физическую оценку."
                    )
                    '''
                    v1_kmh = float(next_track.get("sp") or 0)
                    t_move = estimate_move_start_kmhps(
                        t0=t_curr,
                        t1=t_next,
                        v1_kmh=v1_kmh,
                        min_move_speed=settings.config.getint("Interests", "MIN_MOVE_SPEED"),
                        small_gap_sec=settings.config.getint("Interests", "INTERESTS_MOVE_SMALL_GAP_SEC", fallback=5),
                        max_gap_sec=settings.config.getint("Interests", "INTERESTS_MOVE_MAX_GAP_SEC", fallback=30),
                        A_KMHPS=float(settings.config.get("Interests", "INTERESTS_MOVE_A_KMHPS", fallback="1.26")),
                        clamp_eps=0.1,
                    )
                    time_after = t_move.strftime("%Y-%m-%d %H:%M:%S")
                    logger.info(f"Оцененный time_after: {time_after}")
                    '''
                    time_after = alarm_dt + datetime.timedelta(seconds=120)
                    time_after = time_after.strftime(settings.TIME_FMT)
                else:
                    time_after, last_stop_idx = find_stop_after_lifting(tracks, i + 1, settings, logger, reg_id)
                    if not time_after:
                        time_after = tracks[i + 1].get("gt")
                        last_stop_idx = i + 1

                # Сдвиг фото ПОСЛЕ
                raw_time_after = datetime.datetime.strptime(time_after, settings.TIME_FMT)
                adjusted_time_after = raw_time_after - datetime.timedelta(
                    seconds=settings.config.getint("Interests", "PHOTO_AFTER_SHIFT_SEC"))
                time_after_adj = adjusted_time_after.strftime("%Y-%m-%d %H:%M:%S")

                # «окно интереса» для выгрузки (как у тебя ниже: end_time — это time_30_after)
                time_30_after = (alarm_dt + datetime.timedelta(seconds=sec_after)).strftime("%Y-%m-%d %H:%M:%S")
                end_time = time_after or time_30_after

                interval = get_interest_from_track(
                    tracks[-1],
                    start_time=time_before,
                    end_time=end_time,
                    photo_before_timestamp=time_before,
                    photo_after_timestamp=time_after_adj,
                    reg_id=reg_id,
                )
                geo = track.get("ps")
                interval["report"] = {
                    "cargo_type": cargo_type_alarm,  # уже нормализованный "Контейнер"/"Бункер"
                    "geo": geo,
                    "switches_amount": 1,
                    "switch_events": [{
                        "datetime": alarm_ts_str,
                        "switch": a.get("io_index"),
                        "source": "alarm-gap"
                    }],
                }
                ignore_name = _ignored_geo(geo)
                if ignore_name:
                    logger.info(f"{reg_id}: [IGNORE] Концевик в зоне игнора '{ignore_name}', интерес пропущен.")
                else:
                    loading_intervals.append(interval)
                    first_interest = False
                    logger.info(f"{reg_id}: [ALARM GAP] Добавлен интерес по alarm {alarm_ts_str}: {time_before} → {time_after_adj}")


        # === Старая логика концевиков — без изменений ===
        s1 = track.get("s1")
        timestamp = track.get("gt")
        current_dt = datetime.datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        s1_int = int(s1)

        bits = list(bin(s1_int & 0xFFFFFFFF)[2:].zfill(32))
        bits.reverse()
        min_speed_for_switch_detect = settings.config.getint("Interests", "MIN_SPEED_FOR_SWITCH_DETECT")
        euro_on = bits[euro_bit_idx] == '1'
        kgo_on = (kgo_bit_idx is not None) and (bits[kgo_bit_idx] == '1')
        if euro_on or kgo_on:
            cargo_type = "Бункер" if kgo_on else "Контейнер"
            logger.info(f"{reg_id}: [SWITCH] Срабатывание концевика в {timestamp}, EuroIO(bit {euro_bit_idx})={bits[euro_bit_idx]}" + (f", KGOIO(bit {kgo_bit_idx})={bits[kgo_bit_idx]}" if kgo_bit_idx is not None else ""))

            if int(track.get("sp") or 0) > min_speed_for_switch_detect:
                logger.debug(
                    f"{reg_id}: [SWITCH] Игнор: скорость {int(track.get('sp') or 0)} > {min_speed_for_switch_detect}")
                i += 1
                continue

            # 2) Предчек: стояли ли достаточно до концевика
            # Сравниваем datetime с datetime (t_curr и last_stop_started_at)
            if (last_stop_started_at is None) or ((t_curr - last_stop_started_at).total_seconds() < min_stop_duration):
                dur = 0 if last_stop_started_at is None else (t_curr - last_stop_started_at).total_seconds()
                logger.info(f"{reg_id} [SWITCH] Недостаточная длительность остановки перед концевиком ({dur:.1f}s < {min_stop_duration}s) — игнорируем.")
                i += 1
                continue

            logger.debug(f"{reg_id}: [SWITCH] Принято: {cargo_type} в {timestamp}")
            switch_events = []

            if i >= len(tracks):
                logger.warning(f"{reg_id}: [SWITCH] Индекс {i} вне диапазона треков. Прерывание.")
                break

            # Находим время для фото ДО (Последнее время в окне стабильных остановок)
            max_lookback_seconds = None
            if cargo_type == "Бункер":
                max_lookback_seconds = 1500
            time_before = find_first_stable_stop(
                tracks, i, current_dt, settings, first_interest, start_tracks_search_time, reg_id, max_lookback_seconds)
            if not time_before:
                logger.warning(f"{reg_id}: [BEFORE] Не найдена остановка до сработки концевика в {timestamp}")
                if first_interest:
                    logger.warning(f"{reg_id}: [BEFORE] Это был первый интерес, возвращаемся для получения дополнительных треков")
                    return {"error": "No stop before switch found for first interest"}
                else:
                    time_before = current_dt - datetime.timedelta(seconds=120)
                    time_before = time_before.strftime(settings.TIME_FMT)

            lifting_end_idx = i
            last_switch_index = i

            if euro_on:
                switch_events.append({"datetime": timestamp, "switch": euro_bit_idx})
            if kgo_on:
                switch_events.append({"datetime": timestamp, "switch": kgo_bit_idx})

            # В этом цикле мы перебираем треки и ищем трек, когда погрузка закочена (по скорости и концевику)
            logger.debug(f"{reg_id}: [Конец интереса] Начало интереса найдено. Теперь ищем конец интереса.")
            logger.debug(f"Для начала пойдем вперед по трекам и найдем момент, когда машина двинулась")
            move_started_at = None
            move_started_at_str = None
            # Антизалипание: на части ТС концевик может "залипать" в 1 во время движения.
            # В таком случае не игнорируем скорость полностью — подтверждаем движение
            # по устойчивому превышению MIN_MOVE_SPEED в течение MIN_MOVE_DURATION_SEC.
            move_while_switch_on_started_at = None
            move_while_switch_on_started_at_str = None
            while lifting_end_idx + 1 < len(tracks):
                next_track = tracks[lifting_end_idx + 1]
                next_s1 = next_track.get("s1")
                next_spd = int(next_track.get("sp") or 0)
                try:
                    next_s1_int = int(next_s1)
                except (ValueError, TypeError):
                    break

                next_bits = list(bin(next_s1_int & 0xFFFFFFFF)[2:].zfill(32))
                next_bits.reverse()

                logger.debug(f"{reg_id}: [Конец интереса] Ищем движение после погрузки."
                             f" {next_track.get('gt')}, "
                             f"EuroIO(bit {euro_bit_idx})={next_bits[euro_bit_idx]}" +
                             (f", KGOIO(bit {kgo_bit_idx})={next_bits[kgo_bit_idx]}"
                              if kgo_bit_idx is not None else "") + f", sp={next_spd}")

                min_move_speed = settings.config.getint("Interests", "MIN_MOVE_SPEED")
                min_move_duration = settings.config.getint("Interests", "MIN_MOVE_DURATION_SEC")
                sw_time = next_track.get("gt")
                ts = _to_ts(sw_time)

                # 1) если сработал концевик — фиксируем и продолжаем расширять окно
                if next_bits[euro_bit_idx] == '1' or (kgo_bit_idx is not None and next_bits[kgo_bit_idx] == '1'):
                    # Концевик активен, но скорость уже выше порога.
                    # Подтверждаем движение по времени даже при "залипшем" IO.
                    if next_spd >= min_move_speed:
                        if move_while_switch_on_started_at is None:
                            move_while_switch_on_started_at = ts
                            move_while_switch_on_started_at_str = sw_time
                            logger.debug(
                                f"{reg_id}: [Конец интереса] IO=1, но скорость >= порога. "
                                f"Старт подтверждения движения: {move_while_switch_on_started_at_str}"
                            )
                        elif ts - move_while_switch_on_started_at >= int(min_move_duration):
                            move_started_at = move_while_switch_on_started_at
                            move_started_at_str = move_while_switch_on_started_at_str
                            logger.warning(
                                f"{reg_id}: [Конец интереса] Подтвердили движение при IO=1 "
                                f"(возможное залипание концевика): move_started_at={move_started_at_str}, "
                                f"min_move_duration={min_move_duration}s"
                            )
                            break
                    else:
                        move_while_switch_on_started_at = None
                        move_while_switch_on_started_at_str = None
                    lifting_end_idx += 1
                    if next_bits[euro_bit_idx] == '1':
                        switch_events.append({"datetime": sw_time, "switch": euro_bit_idx})
                    if kgo_bit_idx is not None and next_bits[kgo_bit_idx] == '1':
                        switch_events.append({"datetime": sw_time, "switch": kgo_bit_idx})
                    last_switch_index = lifting_end_idx

                # 2) если скорость низкая — расширяем окно, но сбрасываем накопление «движения»
                elif next_spd < min_move_speed:
                    lifting_end_idx += 1
                    move_started_at = None
                    move_while_switch_on_started_at = None
                    move_while_switch_on_started_at_str = None

                # 3) скорость выше порога — проверяем длительность устойчивого движения
                else:
                    if move_started_at is None:
                        move_started_at = ts
                        move_started_at_str = sw_time
                        logger.debug(f"{reg_id}: [Конец интереса] Убедились, что машина поехала на {move_started_at_str}.")
                    lifting_end_idx += 1
                    logger.debug(f"{reg_id}: [Конец интереса] Проверяем достаточно прошло ли времени - {int(ts - move_started_at) >= int(min_move_duration)}. "
                                 f"cur_time - ({sw_time}), min_move_duration - {min_move_duration}, lifting_end_idx - {lifting_end_idx}.")
                    if ts - move_started_at >= int(min_move_duration):
                        logger.debug(f"{reg_id}: [Конец интереса] Времени прошло достаточно")
                        break

            logger.debug(f"{reg_id}: [Конец интереса] Вышли из цикла поиска движения после последнего концевика. "
                         f"last_switch_index={last_switch_index}, move_started_at={move_started_at_str}")
            time_after, last_stop_idx = find_stop_after_lifting(tracks, last_switch_index + 1, settings, logger, reg_id)
            used_fallback = False

            # Если движение уже подтверждено в текущем цикле (в т.ч. при IO=1),
            # но find_stop_after_lifting не смог подтвердить его вторично,
            # используем подтверждённый момент как time_after.
            if (not time_after) and move_started_at_str:
                logger.warning(
                    f"{reg_id}: [AFTER] find_stop_after_lifting не подтвердил движение, "
                    f"берем подтвержденный момент из основного цикла: {move_started_at_str}"
                )
                time_after = move_started_at_str

            if not time_after:
                # Фоллбэк ТОЛЬКО если давно нет новых треков (последний трек старше 30 минут)
                fmt = "%Y-%m-%d %H:%M:%S"
                last_track_dt = datetime.datetime.strptime(tracks[-1]['gt'], fmt)
                age_sec = (datetime.datetime.now() - last_track_dt).total_seconds()

                if age_sec > 30 * 60:
                    # Телеметрия не обновляется ≥30 мин — применяем страховку
                    time_after = fallback_photo_after_time(tracks, last_switch_index, settings, logger)
                    if not time_after:
                        i = lifting_end_idx + 1
                        continue
                    used_fallback = True
                else:
                    # Треки свежие — погрузка, вероятно, ещё идёт. Ждём обновлений, конец не фиксируем.
                    age_min = age_sec / 60.0
                    logger.info(
                        f"{reg_id}: [AFTER] Пропускаем fallback: последний трек свежий ({age_min:.1f} мин назад, {last_track_dt}). Ждём движения.")
                    if loading_intervals:
                        return {"interests": loading_intervals}
                    else:
                        raise LoadingInProgress
                    #i = lifting_end_idx + 1
                    #continue

            if (last_stop_idx is None) and (not used_fallback):
                logger.warning(f"{reg_id}: [AFTER] Нет last_stop_idx — остановка не найдена после {timestamp}")
                i = lifting_end_idx + 1
                continue

            before_adjust_secs = settings.config.getint("Interests", "INTEREST_EURO_BEFORE_SHIFT_SEC")
            if kgo_on:
                before_adjust_secs = settings.config.getint("Interests", "INTEREST_KGO_BEFORE_SHIFT_SEC")

            if before_adjust_secs:
                logger.debug(f"{reg_id}: Двигаем время начала интереса на {before_adjust_secs}с")
                raw_time_before = datetime.datetime.strptime(time_before, settings.TIME_FMT)
                adjusted_time_before= raw_time_before + datetime.timedelta(seconds=before_adjust_secs)
                time_before = adjusted_time_before.strftime(settings.TIME_FMT)

            after_adjust_secs = settings.config.getint("Interests", "PHOTO_AFTER_SHIFT_SEC")
            if after_adjust_secs:
                raw_time_after = datetime.datetime.strptime(time_after, settings.TIME_FMT)
                adjusted_time_after = raw_time_after - datetime.timedelta(seconds=after_adjust_secs)
                logger.debug(f"{reg_id}: Двигаем время конца интереса на {after_adjust_secs}с")
                time_after = adjusted_time_after.strftime(settings.TIME_FMT)

            last_alarm_dt = datetime.datetime.strptime(tracks[last_switch_index].get("gt"), settings.TIME_FMT)
            time_30_after_dt = last_alarm_dt + datetime.timedelta(seconds=sec_after)
            time_30_after = time_30_after_dt.strftime(settings.TIME_FMT)

            end_time = time_after or time_30_after
            if time_before and time_after:
                logger.info(f"{reg_id}: Интерес найден! {time_before} до {end_time}")
                interval = get_interest_from_track(
                    tracks[-1],
                    start_time=time_before,
                    end_time=end_time,
                    photo_before_timestamp=time_before,
                    photo_after_timestamp=time_after,
                    reg_id=reg_id,
                )
                geo = track.get("ps")
                interval["report"] = {
                    "cargo_type": cargo_type,
                    "geo": geo,
                    "switches_amount": len(switch_events),
                    "switch_events": switch_events
                }
                ignore_name = _ignored_geo(geo)
                if ignore_name:
                    logger.info(f"{reg_id}: [IGNORE] Концевик в зоне игнора '{ignore_name}', интерес пропущен.")
                else:
                    loading_intervals.append(interval)
                first_interest = False
            else:
                logger.info(f"{reg_id}: [SKIP] Пропуск: нет {'time_before' if not time_before else ''}{' и ' if not time_before and not time_after else ''}{'time_after' if not time_after else ''}")

            i = lifting_end_idx + 1
        else:
            i += 1

    return {"interests": loading_intervals}

def _to_ts(gt):
    """Универсально переводим поле времени в timestamp (сек)."""
    if isinstance(gt, (int, float)):
        return float(gt)
    # пробуем ISO8601
    from datetime import datetime
    try:
        return datetime.fromisoformat(gt).timestamp()
    except Exception:
        # запасной формат "YYYY-mm-dd HH:MM:SS"
        return datetime.strptime(gt, "%Y-%m-%d %H:%M:%S").timestamp()

def find_stop_after_lifting(tracks, start_idx, settings, logger=None, reg_id=None):
    """
    Ищем момент НАЧАЛА ДВИЖЕНИЯ после lifting и возвращаем (t_move_str, last_stop_idx).
    Поправки:
      - Учитываем разрежённую телеметрию: первый шаг считает dt от предыдущего трека (start_idx-1), если он есть.
      - Подтверждаем движение по двум (и более) "move"-точкам с проверкой реальной длительности между ними.
      - Внутри разрыва оцениваем t_move через estimate_move_start_kmhps(...).
    """

    if logger is None:
        class _N:
            def debug(self, *a, **k): pass
            def info(self, *a, **k): pass
            def warning(self, *a, **k): pass
        logger = _N()

    min_stop_speed = settings.config.getint("Interests", "MIN_STOP_SPEED")
    min_stop_duration = settings.config.getint("Interests", "MIN_STOP_DURATION_SEC")
    min_move_speed = settings.config.getint("Interests", "MIN_MOVE_SPEED")
    min_move_duration = settings.config.getint("Interests", "MIN_MOVE_DURATION_SEC")
    # ограничение вклада одного шага при очень больших дырках
    sample_gap_cap = settings.config.getint("Interests", "SAMPLE_GAP_CAP_SEC", fallback=20)

    def ts(i: int) -> datetime.datetime:
        return datetime.datetime.strptime(tracks[i]["gt"], "%Y-%m-%d %H:%M:%S")

    def spd(i: int) -> float:
        return float(tracks[i].get("sp") or 0)

    n = len(tracks)
    if not (0 <= start_idx < n):
        logger.warning(f"{reg_id}: [AFTER] start_idx {start_idx} вне диапазона треков")
        return None, None

    # --- ключевая правка: учитывать dt от ПРЕДЫДУЩЕЙ точки, если она есть
    prev_index_for_dt = start_idx - 1 if start_idx - 1 >= 0 else start_idx
    prev_t = ts(prev_index_for_dt)

    stop_active = False
    stop_low_dur = 0.0
    stop_end_idx = None

    move_active = False
    move_dur = 0.0
    move_start_idx = None

    # Для подтверждения по "двум точкам движения"
    first_move_ts = None

    i = start_idx
    while i < n:
        t = ts(i)
        v = spd(i)
        dt_raw = (t - prev_t).total_seconds()
        dt = min(max(dt_raw, 0.0), sample_gap_cap)  # кап для сверхбольших дыр

        logger.debug(f"[КОНЕЦ ИНТЕРЕСА] find_stop_after_lifting. t - {t}, v - {v}")

        is_stop = (v <= min_stop_speed)
        is_move = (v >= min_move_speed)

        # --- зона остановки ---
        if is_stop:
            if not stop_active:
                stop_active = True
                stop_low_dur = 0.0
            stop_low_dur += dt
            stop_end_idx = i
            # низкая скорость сбрасывает накопление "движения"
            move_active = False
            move_dur = 0.0
            move_start_idx = None
            first_move_ts = None

        else:
            # завершение «короткой» псевдо-остановки — не считаем её
            if stop_active and stop_low_dur < min_stop_duration:
                stop_active = False
                stop_low_dur = 0.0
                stop_end_idx = None

            if is_move:
                # 1) подтверждение движения "по времени" (старый критерий, но на реальном dt)
                if not move_active:
                    move_active = True
                    move_dur = 0.0
                    move_start_idx = i
                    # первая "move"-точка для критерия "две точки"
                    first_move_ts = t
                move_dur += dt

                # 2) подтверждение по двум точкам движения (устойчиво к редкой сетке)
                two_points_confirm = False
                if first_move_ts is not None:
                    two_points_confirm = (t - first_move_ts).total_seconds() >= float(min_move_duration)

                if (move_active and move_dur >= float(min_move_duration)) or two_points_confirm:
                    # есть «последний стоп»?
                    if stop_end_idx is None:
                        # формально движения хватило, но валидной остановки перед ним мы не собрали
                        # продолжим сканировать (или можно вернуть t как есть)
                        pass
                    else:
                        # оценим момент старта движения между t0=последний стоп и t1=первая move-точка
                        t0 = ts(stop_end_idx)
                        t1 = ts(move_start_idx)
                        v1 = spd(move_start_idx)
                        dt_gap = (t1 - t0).total_seconds()

                        if dt_gap > 1.0:
                            # оценка внутри разрыва по твоей функции
                            t_move = estimate_move_start_kmhps(
                                t0=t0, t1=t1, v1_kmh=v1,
                                min_move_speed=min_move_speed,
                                small_gap_sec=settings.config.getint("Interests", "INTERESTS_MOVE_SMALL_GAP_SEC", fallback=5),
                                max_gap_sec=settings.config.getint("Interests", "INTERESTS_MOVE_MAX_GAP_SEC", fallback=30),
                                A_KMHPS=float(settings.config.get("Interests", "INTERESTS_MOVE_A_KMHPS", fallback="1.26")),
                                clamp_eps=0.1
                            )
                            t_move_str = t_move.strftime("%Y-%m-%d %H:%M:%S")
                            logger.debug(f"{reg_id}: [AFTER] Оценили момент старта движения с разрывом {dt_gap:.1f}s → {t_move_str}")
                            return t_move_str, stop_end_idx
                        else:
                            # плотная сетка — старт ≈ на первой move-точке
                            return t1.strftime("%Y-%m-%d %H:%M:%S"), stop_end_idx
            else:
                # промежуточная скорость — сбрасываем подтверждение движения
                move_active = False
                move_dur = 0.0
                move_start_idx = None
                first_move_ts = None

        prev_t = t
        i += 1

    logger.warning(f"{reg_id}: [PHOTO AFTER] Не удалось подтвердить движение после lifting (start_idx={start_idx})")
    #return ts(start_idx).strftime(settings.TIME_FMT), start_idx

    return None, None


def fallback_photo_after_time(tracks, last_switch_index, settings, logger=None):
    """
    Страховочный механизм на случай, если не удалось найти стабильную остановку.
    Если с момента последнего срабатывания концевика прошло достаточно времени,
    то возвращаем время после как last_switch_time + 60 сек.
    """
    last_switch_time = datetime.datetime.strptime(tracks[last_switch_index]['gt'], "%Y-%m-%d %H:%M:%S")
    now = datetime.datetime.now()
    max_wait_sec = settings.config.getint("Interests", "MAX_WAIT_TIME_MINUTES") * 60

    if (now - last_switch_time).total_seconds() > max_wait_sec:
        fallback_time = last_switch_time + datetime.timedelta(seconds=60)
        if logger:
            logger.warning(
                f"[AFTER-FALLBACK] Используем страховку: прошло >{max_wait_sec} сек, берём last_switch_time + 60 сек => {fallback_time}"
            )
        return fallback_time.strftime("%Y-%m-%d %H:%M:%S")

    if logger:
        logger.warning(
            f"[AFTER-FALLBACK] Не прошло достаточно времени с момента срабатывания ({last_switch_time}), интерес отклонён"
        )
    return None

def find_first_stable_stop(
    tracks,
    start_index,
    current_dt,
    settings,
    first_interest=False,
    start_tracks_search_time=None,
    reg_id=None,
    max_lookback_seconds=None,
):
    """
    Возвращает:
      - начало ближайшей подтверждённой остановки ДО концевика, либо
      - если в течение POST_CONFIRM_MOVE_WINDOW_SEC после подтверждения было движение (v > MIN_STOP_SPEED),
        начало ПЕРВОЙ стабильной остановки ПОСЛЕ этого движения.

    Ключевые принципы:
      1) Короткая остановка (< MIN_STOP_DURATION_SEC) НЕ тянется через движение — сбрасывается сразу при v > MIN_STOP_SPEED.
      2) Подтверждение длинной остановки производится «протяжённым» движением:
         суммарно v >= MIN_MOVE_SPEED в течение MIN_MOVE_DURATION_SEC после неё.
      3) Время начала — фактический gt первой точки серии стопа (без оценок).
    """

    # ---- Конфигурация
    cfg = settings.config
    if not max_lookback_seconds:
        max_lookback_seconds = cfg.getint("Interests", "MAX_LOOKBACK_SECONDS")
    cutoff_time = current_dt - datetime.timedelta(seconds=max_lookback_seconds)

    min_stop_speed        = cfg.getint("Interests", "MIN_STOP_SPEED")
    min_stop_duration_sec = cfg.getint("Interests", "MIN_STOP_DURATION_SEC")
    min_move_speed        = cfg.getint("Interests", "MIN_MOVE_SPEED")
    min_move_duration_sec = int(cfg.get("Interests", "MIN_MOVE_DURATION_SEC",
                                        fallback=str(min_stop_duration_sec)))
    sample_gap_cap        = cfg.getint("Interests", "SAMPLE_GAP_CAP_SEC", fallback=20)
    post_window_sec       = cfg.getint("Interests", "POST_CONFIRM_MOVE_WINDOW_SEC", fallback=30)

    TIME_FMT = settings.TIME_FMT

    def ts(i: int) -> datetime.datetime:
        return datetime.datetime.strptime(tracks[i]["gt"], TIME_FMT)

    def spd(i: int) -> int:
        v = tracks[i].get("sp")
        try:
            return int(v or 0)
        except Exception:
            return 0

    # ---- Пусто?
    if not tracks:
        logger.warning(f"{reg_id}: [ОСТАНОВКА НЕ НАЙДЕНА] пустой массив треков")
        return None

    # ---- Окно поиска (слева: по времени, справа: по индексу концевика)
    times = [ts(i) for i in range(len(tracks))]
    left  = bisect_left(times, cutoff_time)
    right = max(0, min(start_index, len(tracks) - 1))
    if left > right:
        logger.warning(f"{reg_id}: [ОКНО ПУСТО]")
        return None

    # ---- ФАЗА 1: поиск ближайшей ПОДТВЕРЖДЁННОЙ остановки до концевика
    #
    # Идея: проходим окно слева направо, собираем серии «стопа».
    # - Короткая серия (< min_stop_duration_sec), как только встретилось движение (v > min_stop_speed) — СБРАСЫВАЕМ.
    # - Длинная серия (>= min_stop_duration_sec) может быть «подтверждена», если после неё было ПРОТЯЖЁННОЕ движение
    #   (v >= min_move_speed суммарно >= min_move_duration_sec). Храним «последнюю подтверждённую».
    stop_active = False
    stop_start_i = None
    stop_low_dur = 0.0

    move_after_stop_dur = 0.0  # длительность подтверждающего движения после длинной остановки
    last_confirmed = None      # {'stop_start_i', 'confirm_i', 'confirm_t'}

    prev_t = times[left]
    for i in range(left, right + 1):
        t = times[i]
        dt_raw = (t - prev_t).total_seconds() if i > left else 0.0
        dt    = min(max(dt_raw, 0.0), sample_gap_cap)

        v = spd(i)
        is_stop         = v <= min_stop_speed
        is_move_confirm = v >= min_move_speed

        logger.debug(
            f"{reg_id}: [Поиск начала интереса] i={i}, t={t}, v={v}, "
            f"stop_active={stop_active}, stop_low_dur={stop_low_dur:.1f}, move_dur={move_after_stop_dur:.1f}"
        )

        if is_stop:
            # вошли/продолжаем режим "стоп"
            if not stop_active:
                stop_active  = True
                stop_start_i = i
                stop_low_dur = 0.0
                move_after_stop_dur = 0.0
            stop_low_dur += dt
        else:
            # вышли из режима "стоп"
            if stop_active:
                if stop_low_dur < min_stop_duration_sec:
                    # короткая остановка — сбрасываем немедленно при любом движении > min_stop_speed
                    stop_active = False
                    stop_start_i = None
                    stop_low_dur = 0.0
                    move_after_stop_dur = 0.0

                    # Если это уже «подтверждаемое» движение — начнём накапливать (на случай,
                    # если далее встретится длинная остановка и её нужно будет закрыть).
                    if is_move_confirm:
                        move_after_stop_dur += dt
                else:
                    # остановка была длинной — ждём подтверждающее движение
                    if is_move_confirm:
                        move_after_stop_dur += dt
                        if move_after_stop_dur >= min_move_duration_sec:
                            last_confirmed = {
                                'stop_start_i': stop_start_i,
                                'confirm_i': i,
                                'confirm_t': t,
                            }
                            logger.debug(
                                f"{reg_id}: [CONFIRM] подтверждённая остановка: "
                                f"start={tracks[stop_start_i]['gt']}, confirm@{t}, "
                                f"stop_dur={stop_low_dur:.1f}, move_dur={move_after_stop_dur:.1f}"
                            )
                            # закрываем серию и обнуляем
                            stop_active = False
                            stop_start_i = None
                            stop_low_dur = 0.0
                            move_after_stop_dur = 0.0
                    else:
                        # движение недостаточно быстрое — сбрасываем накопление движения
                        move_after_stop_dur = 0.0

        prev_t = t

    # Если подтверждённой остановки нет — возможен фоллбек на активную длинную серию (если она есть)
    if not last_confirmed:
        if stop_active and stop_low_dur >= min_stop_duration_sec:
            start_gt = tracks[stop_start_i]["gt"]
            logger.debug(f"{reg_id}: [FALLBACK] возвращаем активную длинную остановку: {start_gt}")
            return start_gt
        logger.warning(f"{reg_id}: [ОСТАНОВКА НЕ НАЙДЕНА] подтверждений не нашли")
        return None

    cand_start_i = last_confirmed['stop_start_i']
    cand_start_gt = tracks[cand_start_i]['gt']
    confirm_i = last_confirmed['confirm_i']
    confirm_t = last_confirmed['confirm_t']

    logger.debug(f"{reg_id}: [NEAREST] ближайшая подтверждённая остановка start={cand_start_gt}, confirm@{confirm_t}")

    # ---- ФАЗА 2: пост-окно после подтверждения
    # Если в течение post_window_sec после confirm_t было ЛЮБОЕ движение > min_stop_speed,
    # то ищем первую стабильную остановку после этого движения и возвращаем её начало.
    window_end_t = confirm_t + datetime.timedelta(seconds=post_window_sec)

    # Найти первое движение в пост-окне (v > min_stop_speed)
    j_move = None
    for j in range(confirm_i, right + 1):
        if times[j] > window_end_t:
            break
        if spd(j) > min_stop_speed:
            j_move = j
            break

    if j_move is None:
        logger.debug(f"{reg_id}: [WINDOW] движения в {post_window_sec}s не было — возвращаем {cand_start_gt}")
        return cand_start_gt

    # После первого движения в окне ищем первую стабильную остановку (без ограничений по времени)
    post_stop_active = False
    post_stop_start_i = None
    post_stop_low_dur = 0.0
    prev_t = times[j_move]

    for k in range(j_move + 1, right + 1):
        t = times[k]
        dt_raw = (t - prev_t).total_seconds()
        dt     = min(max(dt_raw, 0.0), sample_gap_cap)
        v = spd(k)

        if v <= min_stop_speed:
            if not post_stop_active:
                post_stop_active  = True
                post_stop_start_i = k
                post_stop_low_dur = 0.0
            post_stop_low_dur += dt

            if post_stop_low_dur >= min_stop_duration_sec:
                start_gt_new = tracks[post_stop_start_i]['gt']
                logger.debug(
                    f"{reg_id}: [SHIFT] движение в окне @ {times[j_move]} ⇒ "
                    f"первая стабильная остановка после него: start={start_gt_new}"
                )
                return start_gt_new
        else:
            if post_stop_active:
                # сброс, если не дотянули до стабильности
                post_stop_active  = False
                post_stop_start_i = None
                post_stop_low_dur = 0.0

        prev_t = t

    # Если после движения в окне так и не нашли новую устойчивую остановку — оставляем исходную
    logger.debug(f"{reg_id}: [SHIFT] после движения в окне новая остановка не найдена — возвращаем {cand_start_gt}")
    return cand_start_gt


def detect_gaps(
    tracks: List[Dict[str, Any]],
    window_start: str,
    window_end: str,
    *,
    min_duration_sec: Optional[int] = None,
    ignore_stationary: Optional[bool] = None,
    edge_ignore: Optional[bool] = None,
    reg_id: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Детектит разрывы в треках внутри окна [window_start, window_end].

    Возвращает отсортированный список словарей:
        [{"gap_start": "YYYY-MM-DD HH:MM:SS", "gap_end": "YYYY-MM-DD HH:MM:SS"}, ...]

    Правила:
      * gap учитывается, если delta между соседними треками >= min_duration_sec;
      * оба конца пары должны находиться внутри окна (edge_ignore);
      * если ignore_stationary и оба соседних трека помечены как стоянка
        (ls == 4 и es != 0) — разрыв пропускаем.

    Треки на входе — это raw-список из cms_gate (`tracks`), у каждого элемента
    есть поле `gt` в формате settings.TIME_FMT. Треки вне окна игнорируются.
    """
    TIME_FMT = settings.TIME_FMT

    if min_duration_sec is None:
        min_duration_sec = settings.config.getint(
            "Interests", "GAP_MIN_DURATION_SEC", fallback=120
        )
    if ignore_stationary is None:
        ignore_stationary = settings.config.getboolean(
            "Interests", "GAP_IGNORE_STATIONARY", fallback=True
        )
    if edge_ignore is None:
        edge_ignore = settings.config.getboolean(
            "Interests", "GAP_EDGE_IGNORE", fallback=True
        )

    if not tracks or not isinstance(tracks, list):
        return []

    try:
        w_start = datetime.datetime.strptime(window_start, TIME_FMT)
        w_end = datetime.datetime.strptime(window_end, TIME_FMT)
    except Exception as e:
        logger.warning(
            f"{reg_id}: detect_gaps: некорректные границы окна "
            f"[{window_start} → {window_end}]: {e}"
        )
        return []

    if w_end <= w_start:
        return []

    parsed: List[Tuple[datetime.datetime, Dict[str, Any]]] = []
    for t in tracks:
        gt = (t or {}).get("gt")
        if not isinstance(gt, str):
            continue
        try:
            dt = datetime.datetime.strptime(gt, TIME_FMT)
        except Exception:
            continue
        if edge_ignore and (dt < w_start or dt > w_end):
            continue
        parsed.append((dt, t))

    parsed.sort(key=lambda x: x[0])

    def _is_stationary(t: Dict[str, Any]) -> bool:
        ls = t.get("ls")
        es = t.get("es")
        try:
            ls_ok = int(ls) == 4
        except Exception:
            ls_ok = False
        try:
            es_ok = int(es) != 0
        except Exception:
            es_ok = False
        return ls_ok and es_ok

    gaps: List[Dict[str, str]] = []
    for i in range(len(parsed) - 1):
        dt_prev, t_prev = parsed[i]
        dt_next, t_next = parsed[i + 1]

        delta = (dt_next - dt_prev).total_seconds()
        if delta < min_duration_sec:
            continue

        if ignore_stationary and _is_stationary(t_prev) and _is_stationary(t_next):
            continue

        gaps.append({
            "gap_start": dt_prev.strftime(TIME_FMT),
            "gap_end": dt_next.strftime(TIME_FMT),
        })

    return gaps


def seconds_since_midnight(dt: datetime.datetime) -> int:
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = dt - midnight
    return int(delta.total_seconds())


def cms_data_get_decorator_async(
    max_retries: int = 30,
    delay: float = 1.0,
    return_json: bool = False,
    retry_results: tuple[int, ...] = (22, 24),   # «временные» коды
):
    """
    Универсальный декоратор для CMS-запросов.
    - Разбирает JSON, чтобы решить — ретраить или нет.
    - По умолчанию возвращает httpx.Response; если return_json=True — dict.
    - НЕ ретраит код 32 (offline) — отдаём наверх как есть.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            attempt = 0
            last_exc: Exception | None = None

            while attempt < max_retries:
                attempt += 1
                try:
                    result = await func(*args, **kwargs)

                    # Поддержим обе ветки: функция вернула Response или сразу dict
                    data = None
                    if isinstance(result, httpx.Response):
                        # сетевые/HTTP ошибки → сразу исключение
                        result.raise_for_status()
                        try:
                            data = result.json()
                        except Exception as je:
                            # кривой JSON — можно сделать ещё одну попытку
                            logger.warning(f"[CMS] JSON parse failed on attempt {attempt}/{max_retries}: {je}")
                            raise
                    elif isinstance(result, dict):
                        data = result
                    else:
                        # неизвестный тип — вернём как есть
                        return result

                    # Если JSON получен — смотрим бизнес-код
                    res_code = data.get("result")
                    if res_code in retry_results:
                        # временная ошибка → ждём и повторяем
                        logger.warning(f"[CMS] result={res_code} → retry {attempt}/{max_retries} after {delay}s")
                        await asyncio.sleep(delay)
                        continue
                    # 32 (offline) — не ретраим, отдаём как есть
                    # остальные коды — считаем «ок» и возвращаем

                    return data if return_json else result

                except Exception as e:
                    last_exc = e
                    if attempt >= max_retries:
                        break
                    logger.warning(f"[CMS] attempt {attempt}/{max_retries} failed: {e}; retry after {delay}s")
                    await asyncio.sleep(delay)

            # все попытки исчерпаны
            if last_exc:
                raise last_exc
            raise RuntimeError(f"[CMS] Failed after {max_retries} attempts without specific exception")

        return wrapper
    return decorator
