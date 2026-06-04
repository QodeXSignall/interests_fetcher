"""
Поиск интересов («погрузок») по сырым трекам и алармам CMS.

Принципы:

1.  Алармы первичны.
    /tracks-alarms возвращает события вида «IO_X был активен в [bTimeStr, eTimeStr]».
    Алармы доходят до сервера почти мгновенно и стабильны (в отличие от треков,
    которые могут «опоздать»). В офлайн-режиме (дамп) этого достаточно; в онлайне
    тот же контракт даёт устойчивость к поздним трекам.

2.  Треки вспомогательны.
    Из треков берётся скорость (sp, в десятых км/ч) и битовая маска IO в `s1`
    для дополнения алармов (на случай если какой-то аларм не пришёл) и для
    определения границ остановки.

3.  Один кластер сработок ⇒ одна погрузка.
    Соседние сработки склеиваются в один кластер, если между ними нет движения.
    Любой трек с sp ≥ MIN_MOVE_SPEED между двумя сработками — это «машина
    переехала к следующему контейнеру», разделяем кластеры.

4.  Четыре параметра на каждую погрузку:
        photo_before  = stop_start                         (только приехали)
        interest_start = max(stop_start, first_fire − 30s) (окно для модели)
        interest_end  = min(stop_end,   last_fire  + 30s)
        photo_after   = stop_end − 1s                       (за секунду до движения)

5.  Зона ответственности: считаем только «погрузки», то есть Евро / КГО
    (т.е. IO, привязанные в truck_info к euro_container_alarm и kgo_container_alarm).
    Остальное (двери, заправки и т.п.) — игнорируем. Заброс контейнера без подъёма
    не должен попадать сюда вовсе, потому что в нём датчик не сработает.

Модуль чисто-функциональный: на вход получает сырые tracks/alarms/truck_info и
датавремя дня, на выход — список интересов. Никаких сетевых запросов / I/O.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable

TIME_FMT = "%Y-%m-%d %H:%M:%S"

# Карта: номер IO (1..4) в truck_info ⇄ номер бита в s1 трека.
# Совпадает с тем, как декодирует существующий код (cms_interface/functions.py):
#   io_to_reg_map = {1: 20, 2: 21, 3: 22, 4: 23}
IO_TO_S1_BIT = {1: 20, 2: 21, 3: 22, 4: 23}
# Обратное: для парсинга atp алармов (atp 19/20/21/22 → IO 1/2/3/4).
ATP_TO_IO = {19: 1, 20: 2, 21: 3, 22: 4}


@dataclass(frozen=True)
class Params:
    """Параметры алгоритма. Дефолты подобраны под К630/К483 13.05 (Евро/Бункер)."""

    # Скорости — в десятых км/ч (как приходит из CMS).
    min_stop_speed: int = 30          # 3.0 км/ч и ниже — «стоит»
    min_move_speed: int = 30          # 3.0 км/ч и выше — «движется»
    max_fire_speed: int = 50          # 5.0 км/ч — выше скорости при сработке отбрасываем (залипание)

    # Минимальная длительность остановки перед первым fire (сек.).
    # Меньше — отбрасываем кластер как мусор.
    min_stop_duration_sec: int = 6

    # Максимальная длительность одного fire-event'а (сек.).
    # Реальный цикл подъёма/опускания — секунды-десятки. Всё, что дольше,
    # это «залипание» датчика / агрегированный CMS-маркер. Отбрасываем —
    # обычно эти длинные алармы перекрывают много РЕАЛЬНЫХ коротких сработок,
    # и если их оставить, мы либо съедим всё в один монстро-кластер,
    # либо подавим real fire-event'ы через dedup. Выкидываем заранее.
    max_fire_duration_sec: int = 90

    # Проверка «настоящее движение в окне вокруг fire».
    # ssp/esp алармов берутся в точке начала/конца — недостаточно: бывает sp=30
    # на старте торможения и sp=80 через секунду. Поэтому смотрим speed-профиль
    # в окне [fire_start - move_check_window_sec, fire_end + move_check_window_sec]
    # и если МЕДИАНА скорости там > move_check_threshold — машина реально едет,
    # сработка отбрасывается. Это безопасно для шумовых скачков на стоянке
    # (один-два высокоскоростных трека среди десятков нулей не двигают медиану).
    move_check_window_sec: int = 15
    move_check_threshold: int = 30  # 3.0 км/ч

    # Жёсткий потолок зазора между fire-event'ами внутри одной погрузки.
    # Главный сепаратор — движение между fire-ами; этот кэп подстраховывает
    # на случай длительной стоянки без движения, чтобы не склеить всю смену.
    intra_loading_max_gap_sec: int = 5 * 60

    # Окно interest_*: ± от первого/последнего fire (но клампится границами стопа).
    # У Бункера длинная подготовка перед первой сработкой (машина приезжает,
    # подходят люди, тянут контейнер) — для него используем stop_start (т.е.
    # interest_start = stop_start) при interest_pad_before_sec_kgo = None.
    # При числе — поведёт себя как Евро.
    interest_pad_before_sec: int = 30
    interest_pad_before_sec_kgo: int | None = None  # None ⇒ stop_start для Бункера
    interest_pad_after_sec: int = 30
    interest_pad_after_sec_kgo: int | None = None   # None ⇒ stop_end для Бункера

    # Радиус игнор-зон (DEPO, МПЗ, свалки и т.п.) в метрах. Интересы,
    # геокоордината которых попадает в этот радиус от любой точки игнора,
    # выкидываются (как разгрузка/база). Список точек передаётся отдельным
    # параметром в find_interests (ignore_points), либо None — тогда фильтра нет.
    ignore_points_tolerance_m: float = 50.0

    # Сколько секунд до движения брать как photo_after.
    photo_after_offset_sec: int = 1

    # Дополнительный fire-source: переходы 0→1 битов IO в треках.
    # Алармы первичны; этот источник нужен только если аларм по каким-то
    # причинам не пришёл (защита от пропусков).
    use_track_io_as_secondary_source: bool = True

    # Если True, отбрасываем fire-event'ы вне рабочих часов (06..22 локального).
    # Сейчас не используется, оставлено как параметр на будущее.
    working_hours_only: bool = False


@dataclass
class FireEvent:
    """Одна активация датчика подъёмника (IO_X был замкнут в [start_dt, end_dt])."""

    io: int                  # 1..4
    cargo: str               # 'euro' | 'kgo'
    start_dt: dt.datetime    # начало активного состояния
    end_dt: dt.datetime      # конец активного состояния
    ssp: int                 # скорость на старте (0.1 км/ч)
    esp: int                 # скорость на конце  (0.1 км/ч)
    source: str              # 'alarm' | 'track'
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Cluster:
    """Сгруппированные fire-event'ы одной погрузки."""

    fires: list[FireEvent]

    @property
    def first_fire_dt(self) -> dt.datetime:
        return min(f.start_dt for f in self.fires)

    @property
    def last_fire_dt(self) -> dt.datetime:
        return max(f.end_dt for f in self.fires)

    @property
    def cargo_type(self) -> str:
        """euro/kgo по «большинству». При равенстве — kgo (бункер приоритетнее в существующем коде)."""
        euro = sum(1 for f in self.fires if f.cargo == "euro")
        kgo = sum(1 for f in self.fires if f.cargo == "kgo")
        return "kgo" if kgo > euro else "euro"

    @property
    def max_fire_speed(self) -> int:
        return max(max(f.ssp, f.esp) for f in self.fires)


# ----------------------------------------------------------------------------
# 1. Извлечение fire-event'ов из алармов и треков
# ----------------------------------------------------------------------------

def _parse_dt(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, TIME_FMT)


def extract_fires_from_alarms(
    alarms: Iterable[dict], *, euro_io: int | None, kgo_io: int | None,
    params: Params,
) -> list[FireEvent]:
    """Из массива алармов CMS вытащить fire-event'ы только нужных IO.

    Сразу фильтруем «мусорные» алармы:
      - слишком длинный интервал (> max_fire_duration_sec) — обычно агрегированный
        CMS-маркер, который перекрывает кучу настоящих коротких сработок;
      - высокая скорость на старте/конце (> max_fire_speed) — «залипание» во время
        движения.
    """

    out: list[FireEvent] = []
    relevant: dict[int, str] = {}
    if euro_io is not None:
        relevant[int(euro_io)] = "euro"
    if kgo_io is not None:
        relevant[int(kgo_io)] = "kgo"
    if not relevant:
        return out

    for a in alarms:
        atp = a.get("atp")
        if not isinstance(atp, int):
            continue
        io = ATP_TO_IO.get(atp)
        if io is None or io not in relevant:
            continue

        bts = a.get("bTimeStr")
        ets = a.get("eTimeStr") or bts
        if not bts or not ets:
            continue
        try:
            sdt = _parse_dt(bts)
            edt = _parse_dt(ets)
        except ValueError:
            continue
        if edt < sdt:
            edt = sdt

        ssp = int(a.get("ssp") or 0)
        esp = int(a.get("esp") or 0)
        if max(ssp, esp) > params.max_fire_speed:
            continue
        if (edt - sdt).total_seconds() > params.max_fire_duration_sec:
            continue

        out.append(FireEvent(
            io=io,
            cargo=relevant[io],
            start_dt=sdt,
            end_dt=edt,
            ssp=ssp,
            esp=esp,
            source="alarm",
            raw=a,
        ))
    out.sort(key=lambda f: f.start_dt)
    return out


def extract_fires_from_tracks(
    tracks: Iterable[dict], *, euro_io: int | None, kgo_io: int | None,
    params: Params,
) -> list[FireEvent]:
    """Из треков восстанавливаем fire-event'ы по переходам 0→1 битов нужных IO.

    Нужно как safety-net на случай не пришедшего аларма. На плотных треках
    (К630, 1Hz) попадание точное; на разрежённых (К483) даёт меньше точек,
    но границы fire-event'а всё равно ловим (с погрешностью ≈ разрешение трека).

    Тоже выкидываем «залипания»: если бит активен > max_fire_duration_sec или
    хоть один трек с активным битом имел sp > max_fire_speed, событие не валидно.
    """

    out: list[FireEvent] = []
    bit_io: dict[int, tuple[int, str]] = {}
    if euro_io is not None:
        bit_io[IO_TO_S1_BIT[int(euro_io)]] = (int(euro_io), "euro")
    if kgo_io is not None:
        bit_io[IO_TO_S1_BIT[int(kgo_io)]] = (int(kgo_io), "kgo")
    if not bit_io:
        return out

    # текущее состояние битов: bit_idx → (start_dt, ssp, max_sp_seen)
    active: dict[int, tuple[dt.datetime, int, int]] = {}
    last_seen: dict[int, tuple[dt.datetime, int]] = {}

    def _bits(s1_value: int) -> list[str]:
        return list(bin(int(s1_value) & 0xFFFFFFFF)[2:].zfill(32))[::-1]

    def _emit_if_valid(io: int, cargo: str,
                       sdt: dt.datetime, edt: dt.datetime,
                       ssp: int, esp: int, max_sp: int) -> None:
        if max_sp > params.max_fire_speed:
            return
        if (edt - sdt).total_seconds() > params.max_fire_duration_sec:
            return
        out.append(FireEvent(
            io=io, cargo=cargo,
            start_dt=sdt, end_dt=edt,
            ssp=ssp, esp=esp,
            source="track",
            raw={"track_max_sp": max_sp},
        ))

    for t in tracks:
        gt = t.get("gt")
        s1 = t.get("s1")
        if not gt or s1 is None:
            continue
        try:
            cur_dt = _parse_dt(gt)
        except ValueError:
            continue
        bits = _bits(int(s1))
        sp = int(t.get("sp") or 0)

        for bit_idx, (io, cargo) in bit_io.items():
            on = bits[bit_idx] == "1"
            cur = active.get(bit_idx)
            if on:
                if cur is None:
                    active[bit_idx] = (cur_dt, sp, sp)
                else:
                    sdt, ssp, max_sp = cur
                    active[bit_idx] = (sdt, ssp, max(max_sp, sp))
                last_seen[bit_idx] = (cur_dt, sp)
            else:
                if cur is not None:
                    sdt, ssp, max_sp = cur
                    edt, esp = last_seen.get(bit_idx, (cur_dt, sp))
                    _emit_if_valid(io, cargo, sdt, edt, ssp, esp, max_sp)
                    active.pop(bit_idx, None)
                    last_seen.pop(bit_idx, None)

    # хвостовые активные интервалы (на конец дня IO так и остался 1) — пропускаем,
    # для нас это не валидные погрузки.

    out.sort(key=lambda f: f.start_dt)
    return out


def merge_fire_events(
    alarm_fires: list[FireEvent], track_fires: list[FireEvent],
) -> list[FireEvent]:
    """Объединить два источника, дедуплицируя пересекающиеся интервалы одного IO.

    Аларм — основной; track-fire добавляем, если он не пересекается ни с одним
    alarm-fire-ом того же IO (по time-window).
    """

    out = list(alarm_fires)

    def overlaps(a: FireEvent, b: FireEvent) -> bool:
        return not (b.start_dt > a.end_dt or a.start_dt > b.end_dt)

    by_io: dict[int, list[FireEvent]] = {}
    for f in out:
        by_io.setdefault(f.io, []).append(f)

    for tf in track_fires:
        same_io = by_io.get(tf.io, [])
        if any(overlaps(tf, af) for af in same_io):
            continue
        out.append(tf)
        by_io.setdefault(tf.io, []).append(tf)

    out.sort(key=lambda f: f.start_dt)
    return out


# ----------------------------------------------------------------------------
# 2. Кластеризация fire-event'ов по «нет движения между ними»
# ----------------------------------------------------------------------------

def _max_sp_between(tracks_sorted: list[tuple[dt.datetime, int]], a: dt.datetime, b: dt.datetime) -> int:
    """Максимальная скорость среди треков с a < gt < b. 0, если треков нет."""
    if not tracks_sorted or b <= a:
        return 0
    # бинарный поиск проще, но кол-во точек на одну межсработную дырку обычно мало.
    # Для плотных треков обходим линейно с маленькой эвристикой.
    import bisect
    keys = [k for k, _ in tracks_sorted]
    lo = bisect.bisect_right(keys, a)
    hi = bisect.bisect_left(keys, b)
    if lo >= hi:
        return 0
    return max(sp for _, sp in tracks_sorted[lo:hi])


def _median_sp_in_window(tracks_sorted: list[tuple[dt.datetime, int]],
                         center_start: dt.datetime, center_end: dt.datetime,
                         pad_sec: int) -> int | None:
    """Медиана скорости в окне [center_start - pad, center_end + pad].

    Используется чтобы отличить РЕАЛЬНОЕ движение (медиана высокая) от шума
    скорости во время стоянки (медиана 0, но 1-2 трека с большим sp).
    Возвращает None если в окне нет треков (некому судить).
    """
    if not tracks_sorted:
        return None
    import bisect
    lo_dt = center_start - dt.timedelta(seconds=pad_sec)
    hi_dt = center_end + dt.timedelta(seconds=pad_sec)
    keys = [k for k, _ in tracks_sorted]
    lo = bisect.bisect_left(keys, lo_dt)
    hi = bisect.bisect_right(keys, hi_dt)
    if lo >= hi:
        return None
    speeds = sorted(sp for _, sp in tracks_sorted[lo:hi])
    return speeds[len(speeds) // 2]


def cluster_fires(
    fires: list[FireEvent],
    tracks_for_speed: list[tuple[dt.datetime, int]],
    params: Params,
) -> list[Cluster]:
    """Разбить fire-event'ы на кластеры по правилам:
    разделитель — либо движение между fire-ами, либо хард-кэп по времени."""

    if not fires:
        return []

    fires = sorted(fires, key=lambda f: f.start_dt)
    clusters: list[Cluster] = []
    cur = [fires[0]]
    for nxt in fires[1:]:
        prev_end = cur[-1].end_dt
        gap = (nxt.start_dt - prev_end).total_seconds()
        moved = _max_sp_between(tracks_for_speed, prev_end, nxt.start_dt) >= params.min_move_speed
        if moved or gap > params.intra_loading_max_gap_sec:
            clusters.append(Cluster(cur))
            cur = [nxt]
        else:
            cur.append(nxt)
    clusters.append(Cluster(cur))
    return clusters


# ----------------------------------------------------------------------------
# 3. Поиск границ остановки вокруг кластера и сборка 4 таймстампов
# ----------------------------------------------------------------------------

@dataclass
class Interest:
    plate: str
    reg_id: str
    cargo_type: str                  # 'euro' | 'kgo'
    photo_before_dt: dt.datetime
    interest_start_dt: dt.datetime
    interest_end_dt: dt.datetime
    photo_after_dt: dt.datetime
    first_fire_dt: dt.datetime
    last_fire_dt: dt.datetime
    fires_count: int
    geo: str | None
    cluster: Cluster
    # True если stop_end не подтверждён реальным движением в треках (машина,
    # возможно, ещё стоит и грузит). Caller решает: подождать новых треков или
    # отдать как есть с fallback-границами.
    is_provisional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plate": self.plate,
            "reg_id": self.reg_id,
            "cargo_type": self.cargo_type,  # 'euro' | 'kgo'
            "photo_before_timestamp": self.photo_before_dt.strftime(TIME_FMT),
            "interest_start": self.interest_start_dt.strftime(TIME_FMT),
            "interest_end": self.interest_end_dt.strftime(TIME_FMT),
            "photo_after_timestamp": self.photo_after_dt.strftime(TIME_FMT),
            "first_fire": self.first_fire_dt.strftime(TIME_FMT),
            "last_fire": self.last_fire_dt.strftime(TIME_FMT),
            "fires_count": self.fires_count,
            "geo": self.geo,
            "is_provisional": self.is_provisional,
        }


def find_stop_bounds(
    tracks_sorted: list[tuple[dt.datetime, int]],
    first_fire_dt: dt.datetime,
    last_fire_dt: dt.datetime,
    params: Params,
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Найти границы остановки, в которой произошёл кластер.

    stop_start: первый трек ≤ first_fire с sp ≤ min_stop_speed,
                идущий после последнего трека с sp > min_stop_speed.
                Иначе говоря, начало стоянки, в которой произошла сработка.

    stop_end:   первый трек > last_fire с sp ≥ min_move_speed.
                То есть момент, когда машина поехала.

    Если границы не нашлись — возвращаем None для соответствующего конца;
    верхний слой решает что делать (обычно — fallback к first_fire / last_fire).
    """

    if not tracks_sorted:
        return None, None

    import bisect
    keys = [k for k, _ in tracks_sorted]

    # --- stop_start: последний "движение" до first_fire, затем первый "стоп" после него
    idx_first_fire = bisect.bisect_right(keys, first_fire_dt)  # exclusive
    last_move_idx = None
    for i in range(idx_first_fire - 1, -1, -1):
        if tracks_sorted[i][1] >= params.min_move_speed:
            last_move_idx = i
            break

    if last_move_idx is None:
        # До first_fire вообще не было движения в имеющихся треках.
        # Считаем stop_start = первый трек дня.
        stop_start = tracks_sorted[0][0]
    else:
        # Первый "стоп"-трек после last_move_idx.
        stop_start = None
        for i in range(last_move_idx + 1, idx_first_fire):
            if tracks_sorted[i][1] <= params.min_stop_speed:
                stop_start = tracks_sorted[i][0]
                break
        if stop_start is None:
            # пограничный кейс: движение продолжалось до first_fire без стопа в треках
            # (вероятно ложный fire или дырка в треках). Поставим за 1 сек до first_fire.
            stop_start = first_fire_dt - dt.timedelta(seconds=1)

    # --- stop_end: первый "движение"-трек после last_fire
    idx_last_fire = bisect.bisect_right(keys, last_fire_dt)
    stop_end = None
    for i in range(idx_last_fire, len(tracks_sorted)):
        if tracks_sorted[i][1] >= params.min_move_speed:
            stop_end = tracks_sorted[i][0]
            break
    # если так и не нашли (машина больше не поехала до конца дня) — оставим None,
    # верхний слой подставит last_fire + pad.

    return stop_start, stop_end


# ----------------------------------------------------------------------------
# 4. Главный «один день для одной машины» пайплайн
# ----------------------------------------------------------------------------

def _ps_at(tracks: list[dict], dt_value: dt.datetime) -> str | None:
    """Координаты ближайшего к dt_value трека (поле ps в формате 'lat,lng')."""
    if not tracks:
        return None
    # ленивая близость — линейный поиск (треков обычно ≤ 40k за день, ок).
    best = None
    best_diff = None
    for t in tracks:
        gt = t.get("gt")
        if not gt:
            continue
        try:
            tdt = _parse_dt(gt)
        except ValueError:
            continue
        diff = abs((tdt - dt_value).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best = t.get("ps")
            if diff == 0:
                break
    return best


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _parse_latlon(s: str) -> tuple[float, float] | None:
    try:
        lat_s, lon_s = s.split(",")
        return float(lat_s.strip()), float(lon_s.strip())
    except Exception:
        return None


def _ignored_zone(geo: str | None, ignore_points: list[dict] | None,
                  default_tolerance_m: float) -> str | None:
    """Возвращает имя ближайшей игнор-зоны или None.

    Каждая точка может задавать свой радиус через поле "radius_m". Если поле
    отсутствует — берётся default_tolerance_m. Это нужно потому что точка
    игнора может быть и точечной (DEPO 50м), и большой (свалка 500м).
    """
    if not geo or not ignore_points:
        return None
    ll = _parse_latlon(geo)
    if ll is None:
        return None
    lat1, lon1 = ll
    best_name = None
    best_score = float("inf")  # минимизируем d/radius — «насколько в зоне»
    for item in ignore_points:
        g = item.get("geo")
        if not g:
            continue
        ll2 = _parse_latlon(g)
        if ll2 is None:
            continue
        radius = float(item.get("radius_m") or default_tolerance_m)
        d = _haversine_m(lat1, lon1, ll2[0], ll2[1])
        if d <= radius and (d / radius) < best_score:
            best_score = d / radius
            best_name = item.get("name")
    return best_name


def find_interests(
    *,
    tracks: list[dict],
    alarms: list[dict],
    truck_info: dict[str, Any],
    plate: str,
    reg_id: str,
    params: Params | None = None,
    ignore_points: list[dict] | None = None,
) -> list[Interest]:
    """Главный энтри-пойнт: на вход сырые tracks + alarms + конфиг машины, на выход — интересы.

    ignore_points: список {"name": "...", "geo": "lat,lon"} — точки игнора (DEPO, свалки).
    Кластеры, геоточка которых ближе чем ignore_points_tolerance_m к любой из них,
    выкидываются. Передавай None если фильтр не нужен.
    """

    p = params or Params()
    euro_io = truck_info.get("euro_container_alarm")
    kgo_io = truck_info.get("kgo_container_alarm")
    try:
        euro_io = int(euro_io) if euro_io is not None else None
    except (TypeError, ValueError):
        euro_io = None
    try:
        kgo_io = int(kgo_io) if kgo_io is not None else None
    except (TypeError, ValueError):
        kgo_io = None

    if not euro_io and not kgo_io:
        return []

    # 0. Подготовка списка (gt, sp) — он нужен и для разрезов кластеров, и для границ стопа.
    tracks_sorted: list[tuple[dt.datetime, int]] = []
    for t in tracks:
        gt = t.get("gt")
        if not gt:
            continue
        try:
            tdt = _parse_dt(gt)
        except ValueError:
            continue
        try:
            sp = int(t.get("sp") or 0)
        except (TypeError, ValueError):
            sp = 0
        tracks_sorted.append((tdt, sp))
    tracks_sorted.sort(key=lambda x: x[0])

    # 1. Fire-event'ы
    alarm_fires = extract_fires_from_alarms(alarms, euro_io=euro_io, kgo_io=kgo_io, params=p)
    track_fires = (
        extract_fires_from_tracks(tracks, euro_io=euro_io, kgo_io=kgo_io, params=p)
        if p.use_track_io_as_secondary_source else []
    )
    fires = merge_fire_events(alarm_fires, track_fires)

    # 1.5. Отбрасываем fire'ы, попавшие в реальное движение.
    # ssp/esp проверяют точку, max_sp в bit-active интервале тоже только bit-active.
    # Если же машина РЕАЛЬНО ехала (медиана скорости в окне ±15с > 3 км/ч), это
    # ложная сработка на ходу, а не «шум» на стоянке.
    if fires and tracks_sorted:
        fires = [
            f for f in fires
            if (lambda m: m is None or m <= p.move_check_threshold)(
                _median_sp_in_window(tracks_sorted, f.start_dt, f.end_dt, p.move_check_window_sec)
            )
        ]

    if not fires:
        return []

    # 2. Кластеры
    clusters = cluster_fires(fires, tracks_sorted, p)

    # 3. Для каждого кластера — стоп-границы и 4 таймстампа
    interests: list[Interest] = []
    for cl in clusters:
        # Отбрасываем «залипание»: если скорость во время любой сработки кластера
        # слишком высокая — это не настоящая погрузка.
        if cl.max_fire_speed > p.max_fire_speed:
            continue

        first_fire = cl.first_fire_dt
        last_fire = cl.last_fire_dt
        stop_start, stop_end = find_stop_bounds(tracks_sorted, first_fire, last_fire, p)

        # Если стоп-границы не нашлись — используем алармы как самозащиту.
        # Запоминаем что stop_end не подтверждён — пометим интерес provisional.
        _stop_end_was_none = stop_end is None
        if stop_start is None:
            stop_start = first_fire - dt.timedelta(seconds=p.interest_pad_before_sec)
        if stop_end is None:
            stop_end = last_fire + dt.timedelta(seconds=p.interest_pad_after_sec)

        # Длительность стопа перед первой сработкой.
        stop_dur_before = (first_fire - stop_start).total_seconds()
        if stop_dur_before < p.min_stop_duration_sec:
            # Слишком быстро после прибытия — вероятно мусор (или мы плохо
            # засекли начало стопа). Не игнорируем полностью — иногда машина
            # реально приезжает и сразу грузит — но залогируем при необходимости.
            # Сейчас пропускаем, чтобы держать precision выше.
            # (можно сделать параметром «строгий режим»)
            pass

        # Применяем формулу 4 таймстампов. Для Бункера допускаем None в pad'ах:
        # тогда интерес тянется на весь стоп (потому что у Бункера долгая
        # подготовка перед первой сработкой и долгое опускание после последней).
        is_kgo = cl.cargo_type == "kgo"
        pad_before = p.interest_pad_before_sec_kgo if is_kgo else p.interest_pad_before_sec
        pad_after = p.interest_pad_after_sec_kgo if is_kgo else p.interest_pad_after_sec

        if pad_before is None:
            interest_start = stop_start
        else:
            interest_start = max(stop_start, first_fire - dt.timedelta(seconds=pad_before))
        if pad_after is None:
            interest_end = stop_end
        else:
            interest_end = min(stop_end, last_fire + dt.timedelta(seconds=pad_after))
        photo_before = stop_start
        photo_after = stop_end - dt.timedelta(seconds=p.photo_after_offset_sec)

        # Защита от инверсий
        if interest_end < interest_start:
            interest_end = interest_start + dt.timedelta(seconds=1)
        if photo_after < photo_before:
            photo_after = photo_before + dt.timedelta(seconds=1)

        geo = _ps_at(tracks, first_fire)

        # Игнор-зоны (DEPO, свалки, МПЗ). Если кластер в зоне — выкидываем.
        if ignore_points:
            zone = _ignored_zone(geo, ignore_points, p.ignore_points_tolerance_m)
            if zone:
                continue

        # Provisional flag: stop_end ещё не подтверждён движением.
        # У caller'а есть два сценария:
        #   1. Историческая обработка (дамп / catch-up после жамминга) —
        #      provisional=False ок, можно зафиксировать с pad-fallback.
        #   2. Лайв-обработка возле «сейчас» — стоит отложить до подтверждения,
        #      потому что машина может ещё грузить.
        provisional = _stop_end_was_none

        interests.append(Interest(
            plate=plate,
            reg_id=reg_id,
            cargo_type=cl.cargo_type,
            photo_before_dt=photo_before,
            interest_start_dt=interest_start,
            interest_end_dt=interest_end,
            photo_after_dt=photo_after,
            first_fire_dt=first_fire,
            last_fire_dt=last_fire,
            fires_count=len(cl.fires),
            geo=geo,
            cluster=cl,
            is_provisional=provisional,
        ))

    return interests
