from typing import List, Dict, Optional
from interests_fetcher.data import settings
import math
import json


def _parse_latlon(s: str) -> tuple[float, float]:
    """Парсит строку 'lat,lon' → (lat, lon) в градусах."""
    try:
        lat_s, lon_s = s.split(",")
        lat, lon = float(lat_s.strip()), float(lon_s.strip())
    except Exception:
        raise ValueError(f"Ожидался формат 'lat,lon', получено: {s!r}")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError(f"Координаты вне допустимого диапазона: {s!r}")
    return lat, lon

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между точками (в метрах) по формуле хаверсина."""
    R = 6_371_008.8  # средний радиус Земли, м
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def find_nearby_name(
    reference_geo: str,
    items: List[Dict[str, str]],
    tolerance_m: float,
    *,
    name_key: str = "name",
    geo_key: str = "geo"
) -> Optional[str]:
    """
    Ищет ближайшую точку из списка в пределах tolerance_m и возвращает её имя.
    items — список словарей вида {name_key: <имя>, geo_key: 'lat,lon'}.
    Если подходящих нет — вернёт None.
    """
    lat1, lon1 = _parse_latlon(reference_geo)

    best_name: Optional[str] = None
    best_dist = float("inf")

    for item in items:
        name = item[name_key]
        lat2, lon2 = _parse_latlon(item[geo_key])
        d = _haversine_m(lat1, lon1, lat2, lon2)
        if d <= tolerance_m and d < best_dist:
            best_dist = d
            best_name = name

    return best_name


def get_ignore_points(ignore_file_path=settings.IGNORE_POINTS_JSON):
    return json.load(open(ignore_file_path, encoding="utf-8"))["ignore_points"]



if __name__ == "__main__":
    res = find_nearby_name("53.656833,55.959151",get_ignore_points(), 50)
    print(res)
