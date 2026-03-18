from typing import List, Optional
from interests_fetcher import functions as main_funcs
import datetime
import logging

logger = logging.getLogger(__name__)

# Допуск: считаем интервалы «слившимися», если зазор <= этой величины
CLIP_EPS_SEC = 1.0

DT_FMT = "%Y-%m-%d %H:%M:%S"


def _try_parse_dt(s: str) -> Optional[datetime.datetime]:
    if not s:
        return None
    for fmt in (DT_FMT, None):
        try:
            if fmt:
                return datetime.datetime.strptime(s, fmt)
            return datetime.datetime.fromisoformat(s)
        except Exception:
            continue
    return None


def _ensure_dt(val, fallback: Optional[datetime.datetime] = None) -> Optional[datetime.datetime]:
    if isinstance(val, datetime.datetime):
        return val
    if isinstance(val, str):
        dt = _try_parse_dt(val)
        if dt:
            return dt
    return fallback


def _day_start(interest: dict) -> datetime.datetime:
    return datetime.datetime(
        int(interest["year"]),
        int(interest["month"]),
        int(interest["day"]),
        0,
        0,
        0,
    )


def _sec_to_dt(interest: dict, sec_key: str) -> Optional[datetime.datetime]:
    sec = interest.get(sec_key)
    if sec is None:
        return None
    try:
        sec_f = float(sec)
    except Exception:
        return None
    return _day_start(interest) + datetime.timedelta(seconds=sec_f)


def _get_start_dt(interest: dict) -> datetime.datetime:
    dt = _ensure_dt(interest.get("start_time"))
    if dt:
        return dt
    dt = _ensure_dt(interest.get("photo_before_timestamp"))
    if dt:
        return dt
    dt = _sec_to_dt(interest, "beg_sec")
    if dt:
        return dt
    # крайний fallback — сейчас, чтобы не падать
    return datetime.datetime.now()


def _get_end_dt(interest: dict) -> datetime.datetime:
    dt = _ensure_dt(interest.get("end_time"))
    if dt:
        return dt
    dt = _ensure_dt(interest.get("photo_after_timestamp"))
    if dt:
        return dt
    dt = _sec_to_dt(interest, "end_sec")
    if dt:
        return dt
    return _get_start_dt(interest)


def _get_photo_before_dt(interest: dict) -> Optional[datetime.datetime]:
    return _ensure_dt(interest.get("photo_before_timestamp")) or _sec_to_dt(interest, "photo_before_sec")


def _get_photo_after_dt(interest: dict) -> Optional[datetime.datetime]:
    return _ensure_dt(interest.get("photo_after_timestamp")) or _sec_to_dt(interest, "photo_after_sec")


def _normalize_interest(interest: dict) -> dict:
    """
    Копируем интерес и добавляем технические поля:
    _start_dt, _end_dt, _pb_dt, _pa_dt + гарантируем beg_sec/end_sec.
    """
    d = dict(interest)
    d["_start_dt"] = _get_start_dt(d)
    d["_end_dt"] = _get_end_dt(d)
    d["_pb_dt"] = _get_photo_before_dt(d)
    d["_pa_dt"] = _get_photo_after_dt(d)

    # гарантируем наличие beg_sec / end_sec
    if "beg_sec" not in d or d["beg_sec"] is None:
        d["beg_sec"] = (d["_start_dt"] - _day_start(d)).total_seconds()
    if "end_sec" not in d or d["end_sec"] is None:
        d["end_sec"] = (d["_end_dt"] - _day_start(d)).total_seconds()

    return d


def _intervals_touch_or_overlap(a: dict, b: dict, eps: float = CLIP_EPS_SEC) -> bool:
    """
    True, если интервалы пересекаются или соприкасаются с допуском eps.
    """
    s1, e1 = float(a["beg_sec"]), float(a["end_sec"])
    s2, e2 = float(b["beg_sec"]), float(b["end_sec"])
    return not (s2 > e1 + eps or s1 > e2 + eps)


def _merge_two(cur: dict, nxt: dict) -> None:
    """
    Слить два интереса в cur.
    """
    reg_id = cur.get("reg_id", "UNKNOWN")
    cur_name = cur.get("name", "unknown")
    nxt_name = nxt.get("name", "unknown")
    
    # Сохраняем исходные временные границы для логирования
    cur_start_before = cur.get("_start_dt")
    cur_end_before = cur.get("_end_dt")
    nxt_start = nxt.get("_start_dt")
    nxt_end = nxt.get("_end_dt")
    
    # Геолокации для проверки
    geo_cur = (cur.get("report") or {}).get("geo")
    geo_nxt = (nxt.get("report") or {}).get("geo")
    
    # временные границы
    cur["beg_sec"] = min(cur["beg_sec"], nxt["beg_sec"])
    cur["end_sec"] = max(cur["end_sec"], nxt["end_sec"])
    cur["_start_dt"] = min(cur["_start_dt"], nxt["_start_dt"])
    cur["_end_dt"] = max(cur["_end_dt"], nxt["_end_dt"])

    # фото-границы
    pb_candidates = [cur.get("_pb_dt"), nxt.get("_pb_dt")]
    pb_candidates = [x for x in pb_candidates if x is not None]
    if pb_candidates:
        cur["_pb_dt"] = min(pb_candidates)

    pa_candidates = [cur.get("_pa_dt"), nxt.get("_pa_dt")]
    pa_candidates = [x for x in pa_candidates if x is not None]
    if pa_candidates:
        cur["_pa_dt"] = max(pa_candidates)

    # события по концевикам
    rep_cur = cur.get("report") or {}
    rep_nxt = nxt.get("report") or {}
    ev_cur = rep_cur.get("switch_events") or []
    ev_nxt = rep_nxt.get("switch_events") or []

    merged_events = []
    seen = set()
    for ev in ev_cur + ev_nxt:
        dt_val = ev.get("datetime")
        sw = ev.get("switch")
        src = ev.get("source")
        key = (dt_val, sw, src)
        if key in seen:
            continue
        seen.add(key)
        merged_events.append(dict(ev))

    if merged_events:
        def ev_key(ev):
            dt = _ensure_dt(ev.get("datetime"))
            return dt or datetime.datetime.min

        merged_events.sort(key=ev_key)

    if merged_events or rep_cur or rep_nxt:
        rep_new = dict(rep_cur)
        # geo/cargo_type при конфликте — от последнего, это терпимо
        rep_new.update(rep_nxt)
        rep_new["switch_events"] = merged_events
        rep_new["switches_amount"] = len(merged_events)
        cur["report"] = rep_new
    
    # Логирование объединения
    cur_start_after = cur.get("_start_dt")
    cur_end_after = cur.get("_end_dt")
    gap_sec = None
    if cur_end_before and nxt_start:
        gap_sec = (nxt_start - cur_end_before).total_seconds()
    
    cur_start_str = cur_start_before.strftime(DT_FMT) if cur_start_before else "N/A"
    cur_end_str = cur_end_before.strftime(DT_FMT) if cur_end_before else "N/A"
    nxt_start_str = nxt_start.strftime(DT_FMT) if nxt_start else "N/A"
    nxt_end_str = nxt_end.strftime(DT_FMT) if nxt_end else "N/A"
    cur_start_after_str = cur_start_after.strftime(DT_FMT) if cur_start_after else "N/A"
    cur_end_after_str = cur_end_after.strftime(DT_FMT) if cur_end_after else "N/A"
    
    gap_info = f"Зазор между интересами: {gap_sec:.1f}с" if gap_sec is not None else "Пересекаются"
    
    logger.info(
        f"{reg_id}: [ОБЪЕДИНЕНИЕ ИНТЕРЕСОВ] Объединены интересы '{cur_name}' и '{nxt_name}'. "
        f"Первый: {cur_start_str} - {cur_end_str}, "
        f"Второй: {nxt_start_str} - {nxt_end_str}, "
        f"{gap_info}, "
        f"Гео первого: {geo_cur}, Гео второго: {geo_nxt}, "
        f"Результат: {cur_start_after_str} - {cur_end_after_str}, "
        f"Всего концевиков: {len(merged_events)}"
    )


def _finalize_interest(cur: dict) -> None:
    """
    Переводим все технические поля обратно в строки/секунды и пересобираем name.
    """
    ds = _day_start(cur)

    st_dt = cur.get("_start_dt") or ds + datetime.timedelta(seconds=float(cur["beg_sec"]))
    en_dt = cur.get("_end_dt") or ds + datetime.timedelta(seconds=float(cur["end_sec"]))
    if en_dt < st_dt:
        # на всякий случай защищаемся от инверсий
        logger.warning("Interest has end before start, correcting: %s", cur.get("name"))
        st_dt, en_dt = sorted((st_dt, en_dt))

    pb_dt = cur.get("_pb_dt") or st_dt
    pa_dt = cur.get("_pa_dt") or en_dt

    cur["start_time"] = st_dt.strftime(DT_FMT)
    cur["end_time"] = en_dt.strftime(DT_FMT)
    cur["photo_before_timestamp"] = pb_dt.strftime(DT_FMT)
    cur["photo_after_timestamp"] = pa_dt.strftime(DT_FMT)

    cur["photo_before_sec"] = (pb_dt - ds).total_seconds()
    cur["photo_after_sec"] = (pa_dt - ds).total_seconds()

    # пересобираем name в том же формате, что и раньше
    plate = cur.get("car_number") or cur.get("plate") or cur.get("reg_id", "UNKNOWN")
    date_str = st_dt.strftime("%Y.%m.%d")
    start_str = st_dt.strftime("%H.%M.%S")
    end_str = en_dt.strftime("%H.%M.%S")
    try:
        cur["name"] = main_funcs.build_interest_name(plate, date_str, start_str, end_str)
    except Exception:
        # если вдруг что-то пошло не так — хотя бы какое-то имя
        cur["name"] = f"{plate}_{date_str} {start_str}-{end_str}"

    # чистим техполя
    for k in ("_start_dt", "_end_dt", "_pb_dt", "_pa_dt"):
        cur.pop(k, None)


def merge_overlapping_interests(interests: List[dict]) -> List[dict]:
    """
    Простое и предсказуемое объединение интересов:
    - приводим все времена к datetime;
    - сортируем по началу;
    - последовательно склеиваем все пересекающиеся или соприкасающиеся (<= CLIP_EPS_SEC) интервалы
      внутри одного reg_id;
    - объединяем switch_events, пересчитываем switches_amount;
    - пересобираем start_time/end_time, photo_* и name.

    Здесь больше нет вырезания/подрезания хвостов относительно концевиков, как было в старой
    логике. Это избавляет от артефактов типа 06:29:26–06:29:27 и дублирующихся интервалов.
    """
    if not interests:
        return []

    # нормализуем и сортируем по времени начала
    norm = [_normalize_interest(i) for i in interests]
    norm.sort(key=lambda x: (x.get("reg_id"), x["_start_dt"], float(x["beg_sec"])))

    merged: List[dict] = []
    cur = norm[0]

    for nxt in norm[1:]:
        same_reg = cur.get("reg_id") == nxt.get("reg_id")
        overlaps = _intervals_touch_or_overlap(cur, nxt)
        
        if same_reg and overlaps:
            # Вычисляем зазор для логирования
            s1, e1 = float(cur["beg_sec"]), float(cur["end_sec"])
            s2, e2 = float(nxt["beg_sec"]), float(nxt["end_sec"])
            gap = s2 - e1 if s2 > e1 else 0
            
            reg_id = cur.get("reg_id", "UNKNOWN")
            cur_start_dt = cur.get("_start_dt")
            cur_end_dt = cur.get("_end_dt")
            nxt_start_dt = nxt.get("_start_dt")
            nxt_end_dt = nxt.get("_end_dt")
            
            cur_start_str = cur_start_dt.strftime(DT_FMT) if cur_start_dt else "N/A"
            cur_end_str = cur_end_dt.strftime(DT_FMT) if cur_end_dt else "N/A"
            nxt_start_str = nxt_start_dt.strftime(DT_FMT) if nxt_start_dt else "N/A"
            nxt_end_str = nxt_end_dt.strftime(DT_FMT) if nxt_end_dt else "N/A"
            
            logger.info(
                f"{reg_id}: [ОБЪЕДИНЕНИЕ ИНТЕРЕСОВ] Обнаружено пересечение/соприкосновение интересов. "
                f"Текущий: {cur.get('name', 'unknown')} ({cur_start_str} - {cur_end_str}), "
                f"Следующий: {nxt.get('name', 'unknown')} ({nxt_start_str} - {nxt_end_str}), "
                f"Зазор: {gap:.1f}с (eps={CLIP_EPS_SEC}с)"
            )
            _merge_two(cur, nxt)
        else:
            _finalize_interest(cur)
            merged.append(cur)
            cur = nxt

    _finalize_interest(cur)
    merged.append(cur)
    return merged
