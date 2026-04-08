# Интеграция с API поиска остановок (find-stops)

Эндпоинт перенесён в сервис **cms_gate**. Актуальное описание:

- **Спецификация API и алгоритма**: репозиторий `cms_gate`, файл `docs/find_stops.md`.
- **Миграция клиентов с interests_fetcher**: `docs/find_stops_cms_gate.md` в этом репозитории.

Кратко: `POST /api/v1/find-stops`, заголовок `Authorization: Bearer <CMS_GATE_API_TOKEN>`, то же JSON тела и ответа, что в исторических примерах ниже (URL и авторизация заменены).

## Минимальный пример (cms_gate)

**Запрос**

```http
POST /api/v1/find-stops
Authorization: Bearer <CMS_GATE_API_TOKEN>
Content-Type: application/json
```

```json
{
  "reg_id": "018270348452",
  "date": "2025-12-17",
  "radius_m": 120.0,
  "sites": [
    {"id": "16174", "lat": 53.72728, "lon": 56.37517}
  ]
}
```

**Ответ** — JSON-массив объектов с полями `site_id`, `lat`, `lon`, `stops` (массив интервалов с `start`, `end`, `duration_sec`, `distance_m`).

Подробности: `cms_gate/docs/find_stops.md`.
