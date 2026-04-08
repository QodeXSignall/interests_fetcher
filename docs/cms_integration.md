### CMS и cms_gate: границы в коде interests_fetcher

**Назначение:** зафиксировать, как в текущей версии устроен доступ к данным CMS. Оглавление: [README.md](README.md). Высокий уровень: [overview.md](overview.md).

**Правило для продакшена:** любое **получение данных из CMS и скачивание видео по интересам** для основного пайплайна — **только через HTTP к `cms_gate`** (`cms_gate_client`), а не через `cms_interface/cms_api`.

---

### 1. Клиент к cms_gate

**`interests_fetcher/cms_gate_client.py`**

Используется `main_operator` и REST (`api.py` для списка устройств и разрешения `reg_id`).

| Операция в коде | HTTP к cms_gate |
|-----------------|-----------------|
| `list_devices` | `GET /api/v1/devices` |
| `get_tracks_and_alarms` | `GET /api/v1/tracks-alarms` |
| `download_clips_for_interest` | `POST /api/v1/download-clips-for-interest` |
| `get_device_status` | `GET /api/v1/devices/{reg_id}/status` |
| `list_all_trucks` / страницы траков | `GET /api/v1/trucks` (vehicle manager) |

Учётные данные CMS в процессе `interests_fetcher` для этих вызовов **не нужны** — их знает только `cms_gate`. Нужны `CMS_GATE_BASE_URL`, `CMS_GATE_API_TOKEN`.

---

### 2. Доменная логика без HTTP к CMS

**`interests_fetcher/cms_interface/functions.py`**

Работает с **готовыми** списками треков и алармов (словари из JSON). Вызывается из `main_operator` после `cms_gate_client.get_tracks_and_alarms`: `prepare_alarms`, `find_interests_by_lifting_switches`, вспомогательные функции. **Исходящих запросов к CMS нет.**

---

### 3. main_operator.py

- `login` — заглушка (`jsession` не используется); доступ к CMS через gate.
- `get_devices_online` → `cms_gate_client.list_devices`.
- `get_interests_async` → `cms_gate_client.get_tracks_and_alarms`, далее только `cms_api_funcs.*` над данными.
- Скачивание клипов по интересу → `cms_gate_client.download_clips_for_interest`.
- Кадры из локальных файлов — `video_utils` (ffmpeg и т.д.), не CMS API.

---

### 4. REST API (`interests_fetcher/api.py`)

- Список устройств и поиск `reg_id` по номеру — **`cms_gate_client`** (устройства с gate, локальный `states.json` для первого шага).
- Эндпоинты с интересами используют `Main` / те же пути, что и пайплайн (данные CMS через gate).
- **find-stops** в этом сервисе отсутствует — только в **cms_gate**.

---

### 5. Модули `cms_interface/cms_api.py`, `cms_http.py`, `limits.py`

Остаются в репозитории как **общая библиотека** вызовов CMS (форматы, совместимость). **Текущий основной сценарий** (`USE_CMS_GATE=1`) их для загрузки треков/клипов/списка устройств **не использует**. Возможны вызовы из **тестов**, **misc/**-скриптов, старого кода — при доработке новых функций предпочтительно расширять **cms_gate** и клиент `cms_gate_client`, а не добавлять прямые вызовы CMS в fetcher.

---

### 6. Конфигурация

- **`interests_fetcher/data/config.cfg`** — лимиты пайплайна, `[Interests]`, `[QT_RM]`, `[Process]` и т.д.
- **`.env`** — `USE_CMS_GATE`, `CMS_GATE_*`, WebDAV, `API_KEY` для входящего API этого сервиса.
- Учётные данные **CMS** в fetcher для основного режима **не требуются** (их использует только `cms_gate`).

---

### 7. См. также

- Контракт шлюза со стороны потребителя (кратко): [cms_gate_rest_api.md](cms_gate_rest_api.md).
- Перенос find-stops: [find_stops_cms_gate.md](find_stops_cms_gate.md).
