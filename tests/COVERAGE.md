# Проверяемые требования

Полный набор запускается через `tests/compose/integration.yaml`: PostgreSQL,
закрытый Garage, Linux FFmpeg/seccomp и настоящий Chrome for Testing. ASGI unit/API-тесты
не подменяют браузерные: последние используют TCP-сервер Uvicorn, DOM, JavaScript,
HTML5 video и реальную обработку исходного MP4.

| Требование | Автотесты |
| --- | --- |
| Регистрация, вход, сессия, роли, блокировка, CSRF, rate limit | `test_accounts_api.py`, `test_account_security.py` |
| Регистрация → загрузка → ожидание обработки → просмотр/перемотка → лайк/комментарий | `test_browser.py` (desktop/mobile), `test_worker.py` |
| Главная, поиск по названию, канал, пагинация, студия | `test_user_screens.py` |
| Public/private, grant и отзыв, роли без обхода приватности, недоступные состояния | `test_playback.py`, `test_social_boundaries.py`, `test_browser.py` |
| Отсутствие утечек через поиск, канал, плейлист, комментарии | `test_social_boundaries.py`, `test_moderation.py` |
| Range, HEAD, If-Range, неверный диапазон, отключение клиента | `test_playback.py` (реальные байты Garage и HTTP seek FFmpeg) |
| Лимиты байтов/диска/частоты, MIME, Content-Length, обрыв и повтор | `test_uploads.py`, `test_upload_storage.py` |
| Повреждённое/слишком длинное/без видео, таймауты и sandbox | `test_worker_media.py` |
| Конкурентный захват, аренда/heartbeat, рестарт, повторная доставка | `test_worker.py` |
| Сбой S3/БД, частичные результаты, защита старого поколения | `test_worker.py`, `test_uploads.py` |
| Очистка сирот и идемпотентное удаление только своих объектов | `test_worker.py` |
| Конкурентные лайки/подписки, состав и порядок плейлиста | `test_social.py`, `test_social_boundaries.py` |
| CSRF на мутациях, IDOR, экранирование XSS и отсутствие исполнения в DOM | `test_social_boundaries.py`, `test_browser.py`, `test_uploads.py` |
| Жалобы, скрытие/восстановление, аудит, роль администратора | `test_moderation.py`, `test_browser.py` |
| Миграции base → head и предыдущая → head, FK/UNIQUE/CHECK, индексы | `conftest.py`, `test_database_schema.py` |
| Закрытый бакет, настоящий multipart/PUT/GET/Range/DELETE | `test_uploads.py`, `test_playback.py`, `test_worker.py` |

Браузерные сценарии не перехватывают и не подменяют ответы приложения. Worker
запускается после наблюдаемого состояния «Ожидаем завершения обработки», чтобы
проверять промежуточное состояние без гонки с очень коротким тестовым видео.
S3-объекты каждого сценария имеют случайный UUID и удаляются его fixture.

Запуск тестов пересоздаёт **только явно разрешённую одноразовую БД** с суффиксом
`_test`; запускать два набора параллельно на одной БД нельзя. Демо/рабочая БД
не используется. При локальном запуске без флагов интеграционные тесты пропускаются;
такой результат не подтверждает полный набор.

Проверки чистой Ubuntu, повторного деплоя/восстановления бэкапа и содержимого ZIP
относятся к пунктам 16–19 TODO. Они не объявляются выполненными по результату pytest.
