# Документация interests_fetcher

| Документ | Содержание |
|----------|------------|
| [overview.md](overview.md) | Назначение сервиса, границы с CMS и cms_gate, правило «только через gate» |
| [services_overview.md](services_overview.md) | Компоненты, процессы, размещение на хосте с CMS |
| [cms_integration.md](cms_integration.md) | Где в коде вызовы `cms_gate_client` и где legacy `cms_interface` |
| [cms_gate_rest_api.md](cms_gate_rest_api.md) | Соответствие клиента fetcher эндпоинтам cms_gate; ссылка на канон в репозитории cms_gate |
| [find_stops_cms_gate.md](find_stops_cms_gate.md) | Find-stops перенесён в cms_gate |
| [DEPLOY_WINDOWS_SERVER.md](DEPLOY_WINDOWS_SERVER.md) | Развёртывание на Windows Server |

Размещение на одном хосте с CMS и `cms_gate`: [overview.md](overview.md), [DEPLOY_WINDOWS_SERVER.md](DEPLOY_WINDOWS_SERVER.md).

Корневой файл [FIND_STOPS_API_INTEGRATION.md](../FIND_STOPS_API_INTEGRATION.md) — краткая ссылка; спецификация find-stops в **cms_gate** `docs/find_stops.md`.

Обзор всей экосистемы (цель, модули, топология): [tracker-docs/README.md](../../tracker-docs/README.md).
