# Назначение и контекст

## Задача

`interests_fetcher` — сервис **анализа интересов** (интервалов работы ТС по трекам и алармам), сверки с выгрузками в WebDAV, REST API и фоновый цикл `main_operator` (скачивание материалов, кадры, загрузка в облако, постановка распознавания).

Он **не** дублирует роль CMS и **не** является шлюзом к CMS. Доступ к данным и операциям CMS для этого сервиса в проде идёт **только через** внешний сервис **`cms_gate`**.

## Границы ответственности

| Слой | Роль |
|------|------|
| **CMS** | Платформа регистраторов: треки, алармы, видео, статусы устройств. |
| **cms_gate** | Единственная точка HTTP к CMS для клиентов: треки/алармы, клипы по интересу, устройства, find-stops, vehicle manager (`/trucks`), лимиты. |
| **interests_fetcher** | Интерпретация JSON треков/алармов, поиск интересов, WebDAV, интеграции с QT_RM и др. |

## Взаимодействие с CMS

**Требование для боевого контура:** переменная окружения `USE_CMS_GATE` (вкл. значениями `1`, `true`, `yes`) и заданные `CMS_GATE_BASE_URL`, `CMS_GATE_API_TOKEN`. Тогда **все** запросы за данными CMS и скачивание клипов по интересу выполняются через **`interests_fetcher/cms_gate_client.py`** к REST `cms_gate`, без прямого `cms_api.login` и без HTTP из `main_operator` в CMS.

Прямые модули `interests_fetcher/cms_interface/cms_api.py` и `cms_http.py` остаются в репозитории для **наследия, тестов и вспомогательных скриптов**; **основной пайплайн** (`main_operator`, актуальный REST в `api.py`) к ним для получения треков/устройств/видео с CMS **не подключается**.

Доменная логика по структурам JSON (`cms_interface/functions.py`: `prepare_alarms`, `find_interests_by_lifting_switches`, …) **не делает HTTP** — она получает уже загруженные списки треков и алармов (сегодня — из ответов `cms_gate`).

## Сопутствующие системы

- **Vehicle manager** ведётся в **cms_gate** (`GET/POST /api/v1/trucks`, SQLite). `interests_fetcher` синхронизирует профили в `states.json` (`sync-trucks`, периодическая задача). Подробно: репозиторий **cms_gate**, `docs/vehicle_manager.md`.
- **find-stops** реализован в **cms_gate** (`POST /api/v1/find-stops`). См. [find_stops_cms_gate.md](find_stops_cms_gate.md).

## Документация в этом каталоге

| Файл | Содержание |
|------|------------|
| [README.md](README.md) | Оглавление |
| [services_overview.md](services_overview.md) | Компоненты и процессы чуть подробнее |
| [cms_integration.md](cms_integration.md) | Где в коде граница с `cms_gate` и legacy CMS-модули |
| [cms_gate_rest_api.md](cms_gate_rest_api.md) | Какой контракт `cms_gate` использует fetcher; ссылка на канон в репозитории cms_gate |

Каноничное описание API и архитектуры шлюза — в репозитории **cms_gate**, каталог `docs/` (`http_api.md`, `overview.md`, `architecture.md`, …).
