### CMS-интеграция: границы и точки входа

**Назначение файла**: зафиксировать все места в текущем коде, где сервис напрямую взаимодействует с CMS, чтобы использовать это как ориентир при выносе логики в отдельный сервис `cms_gate`.

---

### 1. Модули низкого уровня CMS

- **`qt_pvp/cms_interface/cms_http.py`**
  - Глобальный `httpx.AsyncClient` для CMS (`get_cms_async_client`, `close_cms_async_client`).
  - Таймауты, лимиты соединений, HTTP/2.

- **`qt_pvp/cms_interface/limits.py`**
  - Асинхронные семафоры и лимиты:
    - `get_cms_global_sem` — общий лимит одновременных запросов к CMS.
    - `get_device_sem` — лимит на устройство.
    - `get_pages_sem` — параллельная загрузка страниц треков/алармов.
    - `get_frame_sem`, `_get_video_sem_for` — лимиты по кадрам и скачиванию видео.

- **`qt_pvp/cms_interface/cms_api.py`**
  - Обёртки над HTTP API CMS:
    - Аутентификация: `login`.
    - Устройства: `get_online_devices`, `get_offline_devices`, `get_device_status_async`.
    - Треки: `get_device_track_page_async`, `get_device_track_all_pages_async`.
    - Алармы: `get_device_alarm_page_async`, `get_device_alarm_all_pages_async`, `flatten_alarms_pages`.
    - Видео: `get_video`, `download_single_clip_per_channel`, `delete_videos_except`, `execute_download_task`, `wait_and_get_dwn_url`.
    - Кадры: `extract_edge_frames_bytes`.
  - Все функции используют `cms_http` и лимиты из `limits`.

---

### 2. Модули доменной логики, зависящие от данных CMS

- **`qt_pvp/cms_interface/functions.py`**
  - Работает с **данными**, полученными из CMS (JSON треков и алармов), но сам HTTP-запросов не делает.
  - Основные функции:
    - `prepare_alarms` — нормализация алармов, определение типа груза, подготовка к поиску по времени.
    - `find_interests_by_lifting_switches` — поиск интересов по трекам и алармам.
    - `find_stops_near_sites_by_date` — поиск остановок около площадок за выбранный день (использует треки из CMS).
    - Вспомогательные функции (`get_interest_from_track`, `estimate_move_start_kmhps`, и др.).
  - Зависит от формата JSON, который возвращают функции из `cms_api`.

---

### 3. Использование CMS в основном воркере

- **`main_operator.py`**
  - Импорты:
    - `from qt_pvp.cms_interface import cms_http`
    - `from qt_pvp.cms_interface import cms_api`
    - `from qt_pvp.cms_interface import functions as cms_api_funcs`
  - Точки вызова CMS:
    - `Main.login` — вызывает `cms_api.login`, сохраняет `self.jsession`.
    - `Main.get_devices_online` — вызывает `cms_api.get_online_devices(self.jsession)` и парсит `"onlines"`.
    - `Main.get_interests_async`:
      - Параллельно вызывает:
        - `cms_api.get_device_track_all_pages_async(self.jsession, reg_id, start_time, stop_time)`.
        - `cms_api.get_device_alarm_all_pages_async(self.jsession, reg_id, start_time, stop_time)`.
      - Передаёт результаты в `cms_api_funcs.prepare_alarms` и `cms_api_funcs.find_interests_by_lifting_switches`.
    - `Main._process_one_interest`:
      - `cms_api.download_single_clip_per_channel` — скачивание клипов по интересу.
      - `cms_api.delete_videos_except` — удаление временных видеофайлов.
    - `Main.process_frames_before_after`:
      - `cms_api.extract_edge_frames_bytes` — извлечение кадров из видео.
  - В функции `_run` (в конце файла) при завершении mainloop вызывается `cms_http.close_cms_async_client`.

---

### 4. Использование CMS в REST API (`qt_pvp/api.py`)

- **`get_all_devices_from_cms(jsession)`**
  - Импортирует `qt_pvp.cms_interface.cms_api`.
  - Вызывает:
    - `cms_api.get_online_devices`.
    - `cms_api.get_offline_devices`.
  - Объединяет онлайн/оффлайн устройства в один список.

- **`get_reg_id_by_car_num_cms`**
  - Использует `get_all_devices_from_cms` для поиска `reg_id` по госномеру.

- **`resolve_reg_id`**
  - При отсутствии `reg_id` и неуспехе локального поиска:
    - При необходимости логинится в CMS: `cms_api.login`.
    - Вызывает `get_reg_id_by_car_num_cms`.

- **Эндпоинты FastAPI**
  - `POST /compare-interests`
    - Через `_get_main_logged_in` создаёт `Main` и логинится в CMS.
    - Использует `resolve_reg_id` (который может логиниться и ходить в CMS).
    - Вызывает `Main.get_interests_async`, который запрашивает треки/алармы из CMS.
  - `POST /get-interests`
    - Аналогично логинится в CMS и вызывает `Main.get_interests_async`.
  - `POST /find-stops`
    - Логинится через `_get_main_logged_in`, вызывает `resolve_reg_id`.
    - Далее вызывает `cms_funcs.find_stops_near_sites_by_date`, который внутри использует данные треков из CMS.

---

### 5. Прочие использования CMS

- **`misc/get_interests.py`**
  - Для отладки:
    - Создаёт `Main`, вызывает `await inst.login()` (через `cms_api.login`).
    - Напрямую вызывает `cms_api.get_device_alarm_all_pages_async` для анализа алармов.
    - Использует `cms_interface.functions.prepare_alarms` и `Main.get_interests_async`.

- **`qt_pvp/tests/main_tests.py`**
  - Импортирует `qt_pvp.cms_interface.cms_api`.
  - В тесте `test_get_img` (помечен `@SkipTest`) вызывает `cms_api.download_video(...)`.

---

### 6. Конфигурация CMS

- **`qt_pvp/data/settings.py`**
  - Описывает:
    - `cms_host` — базовый URL CMS (schema + ip + port).
    - `cms_login`, `cms_password` — учётные данные CMS (из окружения).
    - Параметры лимитов и поведения:
      - Раздел `[Process]` — `MAX_CMS_CONCURRENT`, `MAX_CMS_PER_DEVICE`, `MAX_DEVICES_CONCURRENT`, `MAX_INTERESTS_PER_DEVICE` и др.
      - Раздел `[Semafor]` — `tracks_page_request_max` и пр.
  - Эти параметры используются в:
    - `qt_pvp/cms_interface/limits.py`.
    - `main_operator.py`.

---

### 7. Итоговая граница для выноса в `cms_gate`

В отдельный сервис `cms_gate` логично вынести:

- Низкоуровневые модули работы с CMS:
  - `cms_http`, `limits`, `cms_api` и связанные с ними настройки из `settings`.
- Логику авторизации и получения данных CMS, к которым сейчас обращается:
  - `main_operator.Main` (`login`, `get_devices_online`, `get_interests_async`, скачивание видео, извлечение кадров).
  - REST-слой в `qt_pvp/api.py` (`get_all_devices_from_cms`, `get_reg_id_by_car_num_cms`, `resolve_reg_id`).
- При этом доменная логика анализа треков/алармов и поиска интересов (`cms_interface/functions.py`, `interest_merge_funcs.py`, очередь `pending_interests` в `functions.py`) останется в `interests_getter` и будет получать данные уже через HTTP/REST от `cms_gate`.

