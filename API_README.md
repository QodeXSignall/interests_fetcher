# interests_fetcher REST API

Структурированная документация (компоненты, CMS, контракт `cms_gate`, миграция find-stops): каталог [docs/](docs/README.md).

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

### POST /find-stops — перенесено в cms_gate

Эндпоинт удалён из этого сервиса. Используйте **`cms_gate`**: `POST /api/v1/find-stops` с заголовком `Authorization: Bearer <CMS_GATE_API_TOKEN>`. Тело запроса и формат ответа те же (`reg_id` или `car_num`, `date`, `sites`, `radius_m`).

Пример:

```bash
curl -X POST "http://localhost:<cms_gate_port>/api/v1/find-stops" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CMS_GATE_API_TOKEN" \
  -d '{"reg_id":"018270348452","date":"2025-12-17","radius_m":120,"sites":[{"id":"1","lat":53.7,"lon":56.3}]}'
```

## Примеры запросов

### С авторизацией (Python)

```python
import requests

headers = {"X-API-Key": "your_secret_api_key_here"}
data = {
    "reg_id": "018270348452",
    "start_time": "2025-12-17 00:00:00",
    "end_time": "2025-12-17 23:59:59",
    "merge_overlaps": True,
}
r = requests.post("http://localhost:8001/get-interests", json=data, headers=headers)
print(r.json())
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




