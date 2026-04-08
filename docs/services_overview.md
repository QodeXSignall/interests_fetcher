# Компоненты и процессы

Контекст и правила взаимодействия с CMS: [overview.md](overview.md).

## Назначение

`interests_fetcher` — разбор «интересов» по трекам/алармам (после получения их через **cms_gate**), сверка с WebDAV, REST API, фоновый `main_operator`.

## Компоненты

| Компонент | Роль |
|-----------|------|
| `main_operator.py` | Опрос онлайн-устройств через `cms_gate_client`, расчёт интересов, скачивание клипов через gate, облако, распознавание |
| `interests_fetcher/api.py` | HTTP: compare/get-interests, синхронизация траков с vehicle manager в cms_gate |
| `cms_gate_client.py` | Единственный HTTP-клиент к **cms_gate** для данных CMS в основном пайплайне |
| `cms_interface/functions.py` | Логика над JSON треков/алармов (без HTTP к CMS) |
| `cms_interface/cms_api.py` | Legacy/скрипты; **не** основной путь при `USE_CMS_GATE=1` — см. [cms_integration.md](cms_integration.md) |

## Зависимость от cms_gate

В проде: `USE_CMS_GATE=1`, `CMS_GATE_BASE_URL`, `CMS_GATE_API_TOKEN`. Все запросы треков, устройств и скачивание клипов — **только** через gate (см. [overview.md](overview.md)).

Локальные пути к видео после `download-clips-for-interest` — на машине **cms_gate**; fetcher должен работать на **том же хосте** с CMS и gate (или общий диск).

Find-stops — только в **cms_gate**: [find_stops_cms_gate.md](find_stops_cms_gate.md).

## Конфигурация

- `interests_fetcher/data/config.cfg` — `[Interests]`, `[Process]`, `[QT_RM]` и др.
- `.env` — `CMS_GATE_*`, WebDAV, `API_KEY` для входящего API этого сервиса.

## Развёртывание

- [DEPLOY_WINDOWS_SERVER.md](DEPLOY_WINDOWS_SERVER.md) — interests_fetcher на Windows.
- **cms_gate:** репозиторий cms_gate, `docs/DEPLOY_WINDOWS_SERVER.md`.
