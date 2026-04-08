# Find-stops и cms_gate

## Что изменилось

Раньше в `interests_fetcher` был маршрут `POST /find-stops` (порт по умолчанию 8001 в примерах). Логика перенесена в **`cms_gate`**: `POST /api/v1/find-stops`.

Причины:

- Треки берутся из CMS; выполнять расчёт в том же сервисе, что уже авторизуется в CMS и отдаёт треки, убирает цепочку «interests_fetcher → cms_gate → треки» для этой операции.
- Единая авторизация с остальными методами шлюза (`Bearer`).

## Отличия для клиентов

| Было (interests_fetcher) | Стало (cms_gate) |
|--------------------------|-------------------|
| URL вида `http://<host>:<port>/find-stops` | `http://<host>:<port>/api/v1/find-stops` (порт — как у экземпляра cms_gate, см. развёртывание) |
| Заголовок `X-API-Key` (если задан `API_KEY`) | `Authorization: Bearer <CMS_GATE_API_TOKEN>` |
| Разрешение `car_num`: `states.json`, затем список устройств через cms_gate | В cms_gate: SQLite **trucks**, затем список устройств CMS, затем fallback строки как `reg_id` |

Тело JSON (поля `reg_id` / `car_num`, `date`, `sites`, `radius_m`) и структура ответа — те же, что описаны для старого API; детали и пороги — в **cms_gate** `docs/find_stops.md` и `docs/configuration.md`.

## Где искать код

- Реализация: репозиторий **cms_gate**, модули `find_stops.py`, `cms_operations.py`, маршрут в `app.py`.
- В **interests_fetcher** маршрут удалён из `interests_fetcher/api.py`; функция `find_stops_near_sites_by_date` удалена из `cms_interface/functions.py`.

## Интеграции вне этого репозитория

Клиенты вроде `qt_analyze` / `FindStopsAPIClient` должны:

1. Указать базовый URL на **cms_gate** и путь `/api/v1/find-stops`.
2. Передавать тот же токен, что и для остальных вызовов `cms_gate`, в заголовке `Authorization`, не `X-API-Key`.
