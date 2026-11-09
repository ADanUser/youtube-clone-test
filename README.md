# YouTube copy

Каркас модульного монолита видеосервиса. Доступны типизированная конфигурация,
HTTP health endpoint, отдельная точка входа worker, Compose-инфраструктура и
предметная схема PostgreSQL с SQLAlchemy 2 и Alembic. Бизнес-функции добавляются
следующими пунктами `TODO.md`.

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

Интеграционные тесты схемы требуют отдельную одноразовую PostgreSQL-БД, имя которой
оканчивается на `_test`. Тесты сами выполняют циклы миграций `base → head` и
`0001 → head`, поэтому их нельзя направлять на рабочую БД:

```bash
YOUTUBE_RUN_POSTGRES_TESTS=1 \
YOUTUBE_DATABASE_URL=postgresql+asyncpg://youtube@localhost:5432/youtube_test \
YOUTUBE_DATABASE_PASSWORD_FILE=/path/to/test-password \
uv run pytest tests/test_database_schema.py
```

S3-реквизиты можно задать парой переменных `YOUTUBE_S3_ACCESS_KEY_ID` и
`YOUTUBE_S3_SECRET_ACCESS_KEY`, но в Compose они должны поступать через защищённые файлы,
указанные в `YOUTUBE_S3_ACCESS_KEY_FILE` и `YOUTUBE_S3_SECRET_KEY_FILE`. Контейнер
`garage-init` использует настройки зоны, ёмкости, имени ключа, бакета и ограниченного
таймаута из `.env.example`; ручные команды инициализации не входят в штатный запуск.

## Docker Compose

Для локального инфраструктурного запуска нужны Docker с Compose plugin и OpenSSL.
Сначала создайте игнорируемые Git локальные секреты, затем поднимите стек:

```bash
scripts/prepare-runtime-secrets.sh
docker compose up --build -d
docker compose ps -a
curl http://127.0.0.1:8080/healthz
```

Наружу публикуется только Nginx на порту `8080`. PostgreSQL, S3/RPC/admin Garage и
приложение доступны только во внутренней Docker-сети. `garage-init` автоматически
назначает и применяет односерверный layout, создаёт закрытый бакет и ключ только с
read/write, сохраняет реквизиты в отдельный том и проверяет HEAD/PUT/GET/Range/DELETE.
Повторный `docker compose up -d` перепроверяет фактическое состояние без смены ключа.
Тот же скрипт создаёт отдельный `app-secret` для HMAC-подписи CSRF-токенов; он передаётся
приложению как Docker secret и не попадает в Git или логи.

## API аккаунтов и каналов

Доступны `/api/v1/auth/csrf`, `register`, `login`, `logout`, `me`, публичный
`/api/v1/channels/{handle}`, редактирование своего канала и административная
смена роли/активности. Изменяющие запросы требуют cookie и заголовок `X-CSRF-Token`,
а также `Origin`, точно совпадающий с `YOUTUBE_PUBLIC_ORIGIN`. Для HTTPS нужно включить
`YOUTUBE_COOKIE_SECURE=true`; текущий Compose использует явно обозначенный локальный HTTP-режим.

Остановить контейнеры без потери данных:

```bash
docker compose down
```

Не добавляйте `-v`: эта опция удалит постоянные тома PostgreSQL, Garage и реквизитов.
