# interests_fetcher REST API

## Авторизация

API защищён через API ключ в заголовке `X-API-Key`.

### Настройка

1. Добавьте в `.env` файл:
```bash
API_KEY=your_secret_api_key_here
```

2. Сгенерировать безопасный ключ можно так:
```bash
openssl rand -hex 32
```

3. При каждом запросе передавайте ключ в заголовке:
```bash
curl -H "X-API-Key: your_secret_api_key_here" http://localhost:8001/get-interests
```

### Dev режим

Если `API_KEY` не установлен в `.env`, API работает **без авторизации** (для разработки).

## Эндпоинты

### POST /compare-interests
Сравнение интересов CMS vs WebDAV

**Body:**
```json
{
  "reg_id": "018270348452",
  "day": "2025.12.17",
  "base_path": "/Tracker/Видео выгрузок"
}
```

**Response:**
```json
{
  "cloud_total": 15,
  "detected_total": 14,
  "new_not_in_cloud": ["interest_name_1"],
  "missing_in_detected": ["interest_name_2"]
}
```

### POST /get-interests
Получение интересов за период

**Body:**
```json
{
  "reg_id": "018270348452",
  "start_time": "2025-12-17 00:00:00",
  "end_time": "2025-12-17 23:59:59",
  "merge_overlaps": true
}
```

**Response:**
```json
{
  "count": 14,
  "interests": [...]
}
```

### POST /find-stops
Поиск остановок возле площадок

**Body:**
```json
{
  "reg_id": "018270348452",
  "date": "2025-12-17",
  "radius_m": 120.0,
  "sites": [
    {"id": "16174", "lat": 53.72728, "lon": 56.37517},
    {"id": "16186", "lat": 53.68652, "lon": 56.34846}
  ]
}
```

**Response:**
```json
[
  {
    "site_id": "16174",
    "lat": 53.72728,
    "lon": 56.37517,
    "stops": [
      {
        "start": "2025-12-17 14:30:00",
        "end": "2025-12-17 14:35:00",
        "duration_sec": 300.0,
        "distance_m": 45.2
      }
    ]
  }
]
```

## Примеры запросов

### С авторизацией (Python):
```python
import requests

headers = {"X-API-Key": "your_secret_api_key_here"}
data = {
    "reg_id": "018270348452",
    "date": "2025-12-17",
    "radius_m": 120,
    "sites": [{"id": "site1", "lat": 53.7, "lon": 56.3}]
}

response = requests.post(
    "http://localhost:8001/find-stops",
    json=data,
    headers=headers
)
print(response.json())
```

### С авторизацией (curl):
```bash
curl -X POST http://localhost:8001/find-stops \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_secret_api_key_here" \
  -d '{
    "reg_id": "018270348452",
    "date": "2025-12-17",
    "radius_m": 120,
    "sites": [{"id": "site1", "lat": 53.7, "lon": 56.3}]
  }'
```

## Ошибки авторизации

**403 Forbidden:**
```json
{
  "detail": "Invalid or missing API key"
}
```

Проверьте:
1. Ключ передан в заголовке `X-API-Key`
2. Значение совпадает с `API_KEY` в `.env`
3. API перезапущен после изменения `.env`




