### cms_gate REST / Job API

**Назначение**: спецификация внешнего HTTP API сервиса `cms_gate`, который будет вызываться из `interests_getter` вместо прямых обращений к CMS.

Базовый URL (пример): `http://cms-gate:9000/api/v1`.

Аутентификация: сервисный токен в заголовке `Authorization: Bearer <SERVICE_TOKEN>`.

---

### Общая модель jobs

- **Создание задачи**
  - `POST /api/v1/jobs/{job_type}`
  - Path-параметр:
    - `job_type: string` — тип задачи (см. ниже).
  - Заголовки:
    - `Authorization: Bearer <SERVICE_TOKEN>`
    - `Content-Type: application/json`
  - Тело запроса:
    - JSON-объект с параметрами конкретной задачи.
  - Успешный ответ `201 Created`:
    ```json
    {
      "job_id": "d3e4d6c5-31c4-4aab-b979-7e3c3a1f2a5b"
    }
    ```

- **Получение статуса задачи**
  - `GET /api/v1/jobs/{job_id}`
  - Путь:
    - `job_id: string (UUID)`
  - Успешный ответ `200 OK`:
    ```json
    {
      "job_id": "d3e4d6c5-31c4-4aab-b979-7e3c3a1f2a5b",
      "job_type": "tracks_alarms",
      "status": "pending",             // pending | running | success | failure
      "created_at": "2026-03-04T10:15:30.123Z",
      "updated_at": "2026-03-04T10:16:01.456Z",
      "result": null,                  // при success — JSON, описанный ниже для каждого типа задачи
      "error": null                    // при failure — объект с описанием ошибки
    }
    ```

---

### Типы задач и их контракты

#### 1. list_devices

- **Назначение**: получить список устройств в CMS с их статусом (online / offline) и базовой информацией.
- **Создание задачи**
  - `POST /api/v1/jobs/list_devices`
  - Тело запроса:
    ```json
    {
      "status": "all"   // "online" | "offline" | "all" (по умолчанию "all")
    }
    ```
- **Результат (`result` в ответе GET /jobs/{job_id})**:
  ```json
  {
    "devices": [
      {
        "did": "108410",          // DevIDNO (reg_id)
        "vid": "K630AX702",       // отображаемый идентификатор/госномер
        "status": "online",       // online | offline
        "raw": { "...": "..." }   // оригинальная запись CMS (опционально)
      }
    ]
  }
  ```

Эта структура совместима с текущей логикой, где ожидаются поля `did` и `vid`.

---

#### 2. tracks_alarms

- **Назначение**: получить за заданный интервал треки и алармы по одному регистратору.
- **Создание задачи**
  - `POST /api/v1/jobs/tracks_alarms`
  - Тело запроса:
    ```json
    {
      "reg_id": "108410",
      "start_time": "2026-02-19 06:00:00",
      "end_time": "2026-02-20 18:00:00"
    }
    ```
  - Формат времени: `YYYY-MM-DD HH:MM:SS` (совпадает с текущим `TIME_FMT`).
- **Результат**:
  ```json
  {
    "tracks": [
      {
        "gt": "2026-02-19 06:15:00",   // время точки (строка)
        "s1": 0,                       // битовая маска входов/выходов
        "sp": 36,                      // скорость * 10
        "ps": "55.123456,37.654321",   // позиция (lat,lon) в строковом виде
        "vid": "K630AX702",
        "dev_idno": "108410",
        "...": "..."                   // прочие поля CMS
      }
    ],
    "alarms": [
      {
        "guid": "....",
        "dev_idno": "108410",
        "atp": 22,
        "atpStr": "IO_4报警",
        "stm": 1708339500000,        // начало UTC в ms
        "etm": 1708339800000,        // конец UTC в ms
        "bTimeStr": "2026-02-19 07:05:00",
        "eTimeStr": "2026-02-19 07:10:00",
        "...": "..."
      }
    ]
  }
  ```

Эти поля соответствуют данным, которые уже обрабатываются в `cms_interface/functions.prepare_alarms` и `find_interests_by_lifting_switches`.

---

#### 3. device_status

- **Назначение**: получить текущий статус конкретного устройства.
- **Создание задачи**
  - `POST /api/v1/jobs/device_status`
  - Тело:
    ```json
    {
      "reg_id": "108410"
    }
    ```
- **Результат**:
  ```json
  {
    "device": {
      "did": "108410",
      "vid": "K630AX702",
      "status": "online",        // online | offline | unknown
      "last_seen": "2026-02-19 06:15:00",
      "raw": { "...": "..." }
    }
  }
  ```

---

#### 4. video_meta (опционально)

- **Назначение**: получить описание видеофайлов за интервал по каналу (обёртка над `get_video` и связанными вызовами).
- **Создание задачи**
  - `POST /api/v1/jobs/video_meta`
  - Тело:
    ```json
    {
      "reg_id": "108410",
      "channel_id": 2,
      "start_time": "2026-02-19 06:00:00",
      "end_time": "2026-02-19 06:10:00"
    }
    ```
- **Результат** (пример):
  ```json
  {
    "files": [
      {
        "file_id": "abc123",
        "start_time": "2026-02-19 06:00:00",
        "end_time": "2026-02-19 06:10:00",
        "channel_id": 2,
        "size_bytes": 123456789,
        "download_url": null      // может быть заполнен задачей download_task
      }
    ]
  }
  ```

---

#### 5. download_task (опционально)

- **Назначение**: создать задачу на подготовку ссылки скачивания видео и/или опрос состояния существующей download-задачи CMS.
- **Создание задачи**
  - `POST /api/v1/jobs/download_task`
  - Тело:
    ```json
    {
      "reg_id": "108410",
      "file_id": "abc123"
    }
    ```
- **Результат**:
  ```json
  {
    "download_url": "http://cms.example.com/download/...",
    "expires_at": "2026-02-19 07:00:00"
  }
  ```

---

### Ошибки

Все эндпоинты в случае ошибок возвращают JSON вида:

```json
{
  "detail": "human readable message",
  "code": "INTERNAL_ERROR",      // или другой машинный код
  "meta": {
    "job_id": "optional",
    "cms_status": 500,
    "cms_error": "..."
  }
}
```

Это позволяет `interests_getter` логировать и различать ошибки CMS и внутренние ошибки cms_gate.

