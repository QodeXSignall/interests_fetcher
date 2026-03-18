from interests_fetcher.data import settings
from datetime import timedelta
import datetime

def estimate_move_start_kmhps(
    t0,                 # datetime: последний наблюдаемый «стоп»-трек
    t1,                 # datetime: первый «едем»-трек
    v1_kmh,             # скорость на t1 (в тех же единицах, что MIN_MOVE_SPEED)
    min_move_speed,     # порог «едем» (км/ч)
    small_gap_sec=3,    # «малый разрыв»
    max_gap_sec=20,     # «большой разрыв»
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
        t_move = t1 - timedelta(seconds=min(1.0, dt / 2.0))
    else:
        # 2) Оценка по «физике» разгона в км/ч/сек
        A = max(A_KMHPS, 1e-6)
        tau_sec = max(0.0, v1_kmh) / A
        t_move = t1 - timedelta(seconds=tau_sec)

    # 3) Очень большая дырка — смягчим, если еле тронулись
    if dt > max_gap_sec and v1_kmh <= min_move_speed * 2:
        t_move = max(t1 - timedelta(seconds=1.0), t0 + timedelta(seconds=clamp_eps))

    # 4) Зажать в рамки окна
    lo = t0 + timedelta(seconds=clamp_eps)
    hi = t1 - timedelta(seconds=clamp_eps)
    if t_move < lo: t_move = lo
    if t_move > hi: t_move = hi

    return t_move


if __name__ == "__main__":
    result = estimate_move_start_kmhps(
        datetime.datetime.strptime("2025-10-08 15:05:36", settings.TIME_FMT),
        datetime.datetime.strptime("2025-10-08 15:05:41", settings.TIME_FMT),
        130,
        10)
    print(result)