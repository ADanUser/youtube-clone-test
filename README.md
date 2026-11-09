# YouTube copy

Каркас модульного монолита видеосервиса. На текущем этапе доступны типизированная
конфигурация, HTTP health endpoint и отдельная точка входа worker. PostgreSQL, Garage,
миграции и бизнес-функции добавляются следующими пунктами `TODO.md`.

## Локальная подготовка

Требуется Python 3.12–3.14 и `uv`.

```bash
uv sync --extra dev --frozen
cp .env.example .env
```

Файл `.env.example` не содержит рабочих секретов. До появления Compose приложение может
запускаться без доступных PostgreSQL и Garage: health endpoint проверяет только процесс.

## Запуск

```bash
uv run youtube-web
curl http://127.0.0.1:8000/healthz
```

Точка входа будущего фонового процесса проверяется отдельно:

```bash
uv run youtube-worker
```

## Проверки качества

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

S3-реквизиты можно задать парой переменных `YOUTUBE_S3_ACCESS_KEY_ID` и
`YOUTUBE_S3_SECRET_ACCESS_KEY`, но в Compose они должны поступать через защищённые файлы,
указанные в `YOUTUBE_S3_ACCESS_KEY_FILE` и `YOUTUBE_S3_SECRET_KEY_FILE`. Будущий
`garage-init` использует настройки зоны, ёмкости, имени ключа, бакета и ограниченного
таймаута из `.env.example`; ручные команды инициализации не входят в штатный запуск.
