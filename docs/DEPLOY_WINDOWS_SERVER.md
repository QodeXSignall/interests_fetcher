# Развёртывание interests_fetcher на Windows Server

Инструкция по установке и запуску **interests_fetcher** (REST API и фоновый `main_operator`) на Windows Server или Windows 10/11.

**Предварительное условие:** развёрнут и доступен **cms_gate** с работающим **HTTP API** (прямые эндпоинты к CMS; RabbitMQ/Postgres/Celery под Job API **не обязательны** — см. **cms_gate** `docs/DEPLOY_WINDOWS_SERVER.md`, `docs/overview.md`).

**Размещение:** **interests_fetcher** и **cms_gate** в связке с обработкой интересов должны работать **на одном хосте с CMS** (или с тем же смонтированным хранилищем), иначе локальные пути к видео, которые отдаёт `cms_gate` после `download-clips-for-interest`, не будут доступны процессам `main_operator` на другой машине.

---

## Содержание

1. [Требования](#1-требования)
2. [Подготовка окружения](#2-подготовка-окружения)
3. [Конфигурация](#3-конфигурация)
4. [Запуск вручную](#4-запуск-вручную)
5. [Запуск как служба Windows (NSSM)](#5-запуск-как-служба-windows-nssm)
6. [Проверка и порты](#6-проверка-и-порты)
7. [Устранение неполадок](#7-устранение-неполадок)

Оглавление документации проекта: [README.md](README.md).

---

## 1. Требования

- **ОС**: Windows Server 2016/2019/2022 (или Windows 10/11 для теста).
- **Python**: 3.8+ (рекомендуется 3.10/3.11).
- **cms_gate** с работающим HTTP API (типично порт **8081**) и тем же `CMS_GATE_API_TOKEN`, что будет в `.env` interests_fetcher.
- Каталог с кодом, например `C:\Services\interests_fetcher`.

---

## 2. Подготовка окружения

Установите Python с [python.org](https://www.python.org/downloads/) (галочка «Add Python to PATH»).

```powershell
cd C:\Services\interests_fetcher
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## 3. Конфигурация

### 3.1 Файл `interests_fetcher/data/config.cfg`

- **[QT_RM]** — `schema`, `host`, `port` сервиса распознавания.
- **[Process]** — лимиты пайплайна (`MAX_GLOBAL_INTERESTS`, `MAX_DEVICES_CONCURRENT`, `MAX_INTERESTS_PER_DEVICE`, `MAX_FRAME_EXTRACT` и т.д.).
- **[Interests]** — параметры поиска интересов (`MAX_LOOKBACK_DAYS`, `MIN_STOP_SPEED` и т.п.).

Пример:

```ini
[QT_RM]
schema = http://
host = ls.qodex.tech
port = 8084
```

### 3.2 Переменные окружения (`.env`)

| Переменная | Назначение | Обязательно |
|------------|------------|-------------|
| `USE_CMS_GATE` | `1` / `true` / `yes` — работа через cms_gate | Да на боевом стенде с шлюзом |
| `CMS_GATE_BASE_URL` | База API cms_gate, например `http://localhost:8081/api/v1` | Да |
| `CMS_GATE_API_TOKEN` | Тот же токен, что `CMS_GATE_API_TOKEN` в cms_gate | Да, если в cms_gate задан токен |
| `API_KEY` | Заголовок `X-API-Key` для входящих запросов к API interests_fetcher | Рекомендуется в проде |
| `qt_rm_login`, `qt_rm_password` | Учётные данные QT_RM | По необходимости |
| `webdav_hostname`, `webdav_login`, `webdav_password` | WebDAV для выгрузок | По необходимости |

**Пример `.env`:**

```env
USE_CMS_GATE=1
CMS_GATE_BASE_URL=http://localhost:8081/api/v1
CMS_GATE_API_TOKEN=ваш_секретный_токен
API_KEY=ваш_секретный_api_key

qt_rm_login=ваш_логин_qt_rm
qt_rm_password=ваш_пароль_qt_rm

webdav_hostname=https://your-webdav-host
webdav_login=ваш_логин_webdav
webdav_password=ваш_пароль_webdav
```

---

## 4. Запуск вручную

Порядок: сначала **HTTP API cms_gate** (достаточно uvicorn с доступом к CMS), затем interests_fetcher. Очередь на стороне cms_gate — опциональна.

**REST API (пример порта 8082):**

```powershell
cd C:\Services\interests_fetcher
.\.venv\Scripts\Activate.ps1
python -m uvicorn interests_fetcher.api:app --host 0.0.0.0 --port 8082 --env-file .env
```

**Фоновый обработчик (`main_operator.py`):**

```powershell
cd C:\Services\interests_fetcher
.\.venv\Scripts\Activate.ps1
python -m dotenv -f .env run -- python main_operator.py
```

При наличии `start_api.bat` можно использовать его для API.

Документация API: `http://<сервер>:8082/docs`.

---

## 5. Запуск как служба Windows (NSSM)

**NSSM:** <https://nssm.cc/download>. Пути замените на свои.

**Daemon — `qt_interests_fetcher_daemon`:**

```cmd
C:\Tools\nssm\win64\nssm.exe install qt_interests_fetcher_daemon "C:\Services\interests_fetcher\.venv\Scripts\python.exe" "-m dotenv -f .env run -- python main_operator.py"
C:\Tools\nssm\win64\nssm.exe set qt_interests_fetcher_daemon AppDirectory C:\Services\interests_fetcher
C:\Tools\nssm\win64\nssm.exe set qt_interests_fetcher_daemon Start SERVICE_AUTO_START
```

**API — `qt_interests_fetcher_api`:**

```cmd
C:\Tools\nssm\win64\nssm.exe install qt_interests_fetcher_api "C:\Services\interests_fetcher\.venv\Scripts\python.exe" "-m dotenv -f .env run -- uvicorn interests_fetcher.api:app --host 0.0.0.0 --port 8082"
C:\Tools\nssm\win64\nssm.exe set qt_interests_fetcher_api AppDirectory C:\Services\interests_fetcher
C:\Tools\nssm\win64\nssm.exe set qt_interests_fetcher_api Start SERVICE_AUTO_START
```

Запуск после cms_gate:

```cmd
sc start qt_interests_fetcher_daemon
sc start qt_interests_fetcher_api
```

---

## 6. Проверка и порты

| Сервис | Порт / проверка |
|--------|------------------|
| interests_fetcher API | **8082** (или заданный) → `http://<сервер>:8082/docs` |
| Daemon | Служба в состоянии RUNNING, логи процесса |

cms_gate обычно на **8081** — см. документацию cms_gate.

---

## 7. Устранение неполадок

- **Нет связи с cms_gate:** `USE_CMS_GATE=1`, корректный `CMS_GATE_BASE_URL`, совпадение `CMS_GATE_API_TOKEN` с cms_gate, доступность `http://<хост>:8081/docs`.
- **Ошибки `config.cfg`:** кодировка, секция `[QT_RM]`, пути к `states.json` при необходимости.
- **Служба не стартует:** логи NSSM, тот же запуск вручную в консоли для текста ошибки.

Поиск остановок (`find-stops`) выполняется в **cms_gate**, не в interests_fetcher — см. `cms_gate/docs/find_stops.md` и `docs/find_stops_cms_gate.md` здесь.
