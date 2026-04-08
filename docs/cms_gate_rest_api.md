### Справочник: cms_gate для разработчиков interests_fetcher

**Зачем этот файл в репозитории interests_fetcher:** каноничная спецификация HTTP API живёт в репозитории **cms_gate** (`docs/http_api.md`, `docs/overview.md`, OpenAPI `/docs`). Здесь — **то, что нужно при интеграции fetcher**: соответствие вызовов `cms_gate_client` эндпоинтам и отсылка к полной документации, без дублирования длинных контрактов.

**Аутентификация:** `Authorization: Bearer <CMS_GATE_API_TOKEN>` (тот же токен, что настроен в `cms_gate`).

**Базовый URL:** переменная `CMS_GATE_BASE_URL` (пример: `http://localhost:8081/api/v1`).

---

### Что реально вызывает interests_fetcher

| `cms_gate_client` | Метод HTTP | Путь |
|-------------------|------------|------|
| `list_devices` | GET | `/devices` |
| `get_tracks_and_alarms` | GET | `/tracks-alarms` |
| `download_clips_for_interest` | POST | `/download-clips-for-interest` |
| `get_device_status` | GET | `/devices/{reg_id}/status` |
| `list_trucks_page` / обход страниц | GET | `/trucks` |

Остальные методы клиента — см. исходный файл `interests_fetcher/cms_gate_client.py`.

---

### Job API (`POST /jobs/...`, `GET /jobs/{id}`)

В **interests_fetcher** основной путь — **прямые** эндпоинты выше (без очереди). Job API в шлюзе дублирует часть операций через Celery; для развёртывания и контрактов см. **cms_gate** `docs/http_api.md`, `docs/DEPLOY_WINDOWS_SERVER.md`.

---

### Полная документация cms_gate

В репозитории **cms_gate**, каталог `docs/`: `http_api.md`, `architecture.md`, `configuration.md`, `find_stops.md`, `vehicle_manager.md`, OpenAPI на запущенном экземпляре (`/docs`).

Ошибки и коды ответов — как у любого FastAPI/OpenAPI сервиса; детали в репозитории шлюза.
