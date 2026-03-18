#!/bin/bash

# Переходим в директорию проекта
cd "$(dirname "$0")"

# Активируем виртуальное окружение
source .venv/bin/activate

# Запускаем FastAPI с uvicorn
python -m uvicorn interests_fetcher.api:app --host 0.0.0.0 --port 8001 --env-file .env

