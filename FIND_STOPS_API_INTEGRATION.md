# Интеграция с API поиска остановок (find-stops)

## Описание
API для поиска остановок транспорта возле заданных площадок за конкретную дату. 
Анализирует GPS-треки регистратора и определяет, где и когда машина останавливалась 
рядом с указанными координатами.

## Эндпоинт
```
POST http://<HOST>:8001/find-stops
```

## Авторизация
Обязательный заголовок:
```
X-API-Key: <ваш_api_ключ>
```

## Формат запроса

### Headers
```
Content-Type: application/json
X-API-Key: your_secret_key_here
```

### Body (JSON)
```json
{
  "reg_id": "018270348452",           // ID регистратора (одно из двух: reg_id ИЛИ car_num)
  "car_num": "K630AX702",             // Госномер автомобиля (одно из двух: reg_id ИЛИ car_num)
  "date": "2025-12-17",               // Дата в формате YYYY-MM-DD (обязательно)
  "radius_m": 120.0,                  // Радиус поиска в метрах (опционально, по умолчанию 100м)
  "sites": [                          // Массив площадок (обязательно)
    {
      "id": "site_001",               // Уникальный ID площадки
      "lat": 53.72728,                // Широта
      "lon": 56.37517                 // Долгота
    },
    {
      "id": "site_002",
      "lat": 53.68652,
      "lon": 56.34846
    }
  ]
}
```

**Важно:** Нужно указать **либо `reg_id`, либо `car_num`** (одно из двух обязательно).
- `reg_id` — прямой ID регистратора (например "018270348452"), используется напрямую
- `car_num` — госномер автомобиля (например "K630AX702"), API автоматически найдёт reg_id:
  1. Сначала поиск в локальной БД (`states.json`)
  2. Если не найден — запрос через `cms_gate` (поиск среди всех устройств по полю `vid`)
  3. Если не найден нигде → ошибка 404

## Формат ответа

### Success (200 OK)
```json
[
  {
    "site_id": "site_001",
    "lat": 53.72728,
    "lon": 56.37517,
    "stops": [                         // Массив остановок (может быть пустым)
      {
        "start": "2025-12-17 14:30:15",    // Время начала остановки
        "end": "2025-12-17 14:35:42",      // Время окончания остановки
        "duration_sec": 327.0,              // Длительность в секундах
        "distance_m": 45.2                  // Расстояние от площадки в метрах
      }
    ]
  },
  {
    "site_id": "site_002",
    "lat": 53.68652,
    "lon": 56.34846,
    "stops": []                        // Нет остановок рядом с этой площадкой
  }
]
```

**Важно:** Для каждой площадки возвращается **только одна ближайшая остановка** 
(с минимальным `distance_m`). Если остановок не было — массив `stops` будет пустым.

### Ошибки

**403 Forbidden** - неверный API ключ:
```json
{
  "detail": "Invalid or missing API key"
}
```

**404 Not Found** - регистратор с указанным госномером не найден:
```json
{
  "detail": "Registrator with car number 'K999XX999' not found"
}
```

**422 Unprocessable Entity** - неверный формат данных или не указан reg_id/car_num:
```json
{
  "detail": [
    {
      "loc": ["body", "car_num"],
      "msg": "Either reg_id or car_num must be provided",
      "type": "value_error"
    }
  ]
}
```

## Пример кода (Python)

### Базовый запрос
```python
import requests
from typing import Optional

API_URL = "http://localhost:8001/find-stops"
API_KEY = "your_secret_api_key_here"

def find_stops(date: str, sites: list, reg_id: Optional[str] = None, 
               car_num: Optional[str] = None, radius_m: float = 120.0):
    """
    Поиск остановок возле площадок
    
    Args:
        date: Дата в формате "YYYY-MM-DD"
        sites: Список площадок [{"id": "...", "lat": ..., "lon": ...}, ...]
        reg_id: ID регистратора (например "018270348452") - одно из двух
        car_num: Госномер автомобиля (например "K630AX702") - одно из двух
        radius_m: Радиус поиска в метрах
    
    Returns:
        list: Список площадок с остановками
    """
    if not reg_id and not car_num:
        raise ValueError("Either reg_id or car_num must be provided")
    
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY
    }
    
    payload = {
        "date": date,
        "radius_m": radius_m,
        "sites": sites
    }
    
    if reg_id:
        payload["reg_id"] = reg_id
    else:
        payload["car_num"] = car_num
    
    response = requests.post(API_URL, json=payload, headers=headers)
    response.raise_for_status()
    
    return response.json()


# Использование
sites = [
    {"id": "yard_1", "lat": 53.72728, "lon": 56.37517},
    {"id": "yard_2", "lat": 53.68652, "lon": 56.34846},
]

# Вариант 1: через reg_id
result = find_stops(
    date="2025-12-17",
    sites=sites,
    reg_id="018270348452",
    radius_m=150.0
)

# Вариант 2: через госномер
result = find_stops(
    date="2025-12-17",
    sites=sites,
    car_num="K630AX702",
    radius_m=150.0
)

# Обработка результата
for site in result:
    if site["stops"]:
        stop = site["stops"][0]  # Всегда только одна остановка
        print(f"Площадка {site['site_id']}: остановка с {stop['start']} по {stop['end']}")
        print(f"  Длительность: {stop['duration_sec']}с, расстояние: {stop['distance_m']}м")
    else:
        print(f"Площадка {site['site_id']}: остановок не найдено")
```

### С обработкой ошибок
```python
import requests
from typing import Optional

def find_stops_safe(reg_id: str, date: str, sites: list, radius_m: float = 120.0) -> Optional[list]:
    """Безопасный вызов API с обработкой ошибок"""
    try:
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": API_KEY
        }
        
        payload = {
            "reg_id": reg_id,
            "date": date,
            "radius_m": radius_m,
            "sites": sites
        }
        
        response = requests.post(API_URL, json=payload, headers=headers, timeout=30)
        
        if response.status_code == 403:
            print("Ошибка авторизации: проверьте API_KEY")
            return None
            
        if response.status_code == 422:
            print(f"Неверный формат данных: {response.json()}")
            return None
            
        response.raise_for_status()
        return response.json()
        
    except requests.exceptions.Timeout:
        print("Таймаут запроса (>30 сек)")
        return None
    except requests.exceptions.ConnectionError:
        print("Ошибка соединения с API")
        return None
    except Exception as e:
        print(f"Неожиданная ошибка: {e}")
        return None
```

### Асинхронный вариант (aiohttp)
```python
import aiohttp
import asyncio

async def find_stops_async(reg_id: str, date: str, sites: list, radius_m: float = 120.0):
    """Асинхронный запрос к API"""
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY
    }
    
    payload = {
        "reg_id": reg_id,
        "date": date,
        "radius_m": radius_m,
        "sites": sites
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(API_URL, json=payload, headers=headers) as response:
            response.raise_for_status()
            return await response.json()

# Использование
result = asyncio.run(find_stops_async("018270348452", "2025-12-17", sites))
```

## Примеры запросов (curl)

### Через reg_id
```bash
curl -X POST http://localhost:8001/find-stops \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_secret_api_key_here" \
  -d '{
    "reg_id": "018270348452",
    "date": "2025-12-17",
    "radius_m": 120.0,
    "sites": [
      {"id": "site_1", "lat": 53.72728, "lon": 56.37517},
      {"id": "site_2", "lat": 53.68652, "lon": 56.34846}
    ]
  }'
```

### Через госномер (car_num)
```bash
curl -X POST http://localhost:8001/find-stops \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_secret_api_key_here" \
  -d '{
    "car_num": "K630AX702",
    "date": "2025-12-17",
    "radius_m": 120.0,
    "sites": [
      {"id": "site_1", "lat": 53.72728, "lon": 56.37517},
      {"id": "site_2", "lat": 53.68652, "lon": 56.34846}
    ]
  }'
```

## Ограничения и особенности

1. **Одна остановка на площадку**: API возвращает только ближайшую остановку для каждой площадки
2. **Формат даты**: Строго `YYYY-MM-DD` (с дефисами, не с точками)
3. **Таймаут**: Запрос может занять 10-30 секунд при большом количестве треков
4. **Радиус**: Рекомендуемый диапазон 50-300 метров
5. **Координаты**: Широта/долгота в десятичных градусах (WGS84)
6. **Минимальная длительность остановки**: Определяется конфигурацией сервера (обычно 6 секунд)
7. **Порог скорости остановки**: Определяется конфигурацией сервера (обычно 3 км/ч)

## Типичные сценарии использования

### Проверка посещения площадок по маршруту
```python
# У вас есть маршрут с 50 площадками
route_sites = load_route_from_db(route_id=123)

# Проверяем какие площадки были посещены
result = find_stops(
    reg_id="018270348452",
    date="2025-12-17",
    sites=route_sites,
    radius_m=100.0
)

# Фильтруем только посещенные
visited = [s for s in result if s["stops"]]
not_visited = [s for s in result if not s["stops"]]

print(f"Посещено: {len(visited)} из {len(route_sites)}")
```

### Валидация времени прибытия
```python
# Проверяем соответствие плану
for site in result:
    if site["stops"]:
        actual_time = site["stops"][0]["start"]
        planned_time = get_planned_time(site["site_id"])
        
        delta_minutes = calculate_delta(actual_time, planned_time)
        if abs(delta_minutes) > 15:
            print(f"⚠️ Площадка {site['site_id']}: отклонение {delta_minutes} минут")
```

### Экспорт результатов в CSV
```python
import csv
from datetime import datetime

def export_stops_to_csv(result: list, filename: str):
    """Экспорт результатов в CSV файл"""
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Site ID', 'Latitude', 'Longitude', 'Visit Status', 
                        'Arrival Time', 'Departure Time', 'Duration (min)', 'Distance (m)'])
        
        for site in result:
            if site['stops']:
                stop = site['stops'][0]
                writer.writerow([
                    site['site_id'],
                    site['lat'],
                    site['lon'],
                    'Visited',
                    stop['start'],
                    stop['end'],
                    round(stop['duration_sec'] / 60, 1),
                    round(stop['distance_m'], 1)
                ])
            else:
                writer.writerow([
                    site['site_id'],
                    site['lat'],
                    site['lon'],
                    'Not visited',
                    '', '', '', ''
                ])

# Использование
export_stops_to_csv(result, 'route_report_2025-12-17.csv')
```

## Troubleshooting

### Проблема: Получаю 403 Forbidden
**Решение:** 
- Проверьте наличие заголовка `X-API-Key`
- Убедитесь, что значение совпадает с `API_KEY` в `.env` на сервере
- Перезапустите API после изменения `.env`

### Проблема: API возвращает пустые остановки для всех площадок
**Возможные причины:**
- Неверный `reg_id` (машина не выезжала в эту дату)
- Неверная дата (формат должен быть `YYYY-MM-DD`)
- Слишком маленький радиус поиска (попробуйте увеличить до 200-300м)
- Координаты площадок указаны неверно

### Проблема: Таймаут запроса
**Решение:**
- Увеличьте timeout в запросе до 60 секунд
- Уменьшите количество площадок в одном запросе (рекомендуется до 50)
- Убедитесь, что CMS сервер доступен

### Проблема: Неверное время в результатах
**Решение:**
- Проверьте часовой пояс на сервере API
- Время в ответе всегда в том же часовом поясе, что и треки в CMS
- При необходимости конвертируйте в нужный часовой пояс на клиенте

## Примечания для разработчиков

1. **Кэширование**: API не кэширует результаты. Каждый запрос обращается к `cms_gate`.
2. **Rate limiting**: Не реализован. Рекомендуется ограничивать запросы на стороне клиента.
3. **Batch requests**: Для обработки нескольких дат используйте асинхронные запросы.
4. **Координаты**: API принимает координаты в любом разумном диапазоне, но рекомендуется валидировать их перед отправкой.



