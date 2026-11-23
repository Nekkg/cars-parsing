# Локальный запуск

Краткая инструкция; полная конфигурация и условия — [DEVELOPMENT.md](DEVELOPMENT.md). Приложение расположено в `reference-parser-daa/`; команды выполняются из него. Во время документирования 9–10 октября 2026 года они не выполнялись.

Нужны Docker Desktop с Linux containers и локальный `.env`. Для нового окружения задайте обязательные Django/PostgreSQL-параметры, токен Telegram и отдельный `DJANGO_SUPERUSER_PASSWORD` для `setup_local`. Готового `.env.example` в дереве нет; [шаблон](DEVELOPMENT.md#2-конфигурация).

```powershell
docker compose build bot parsers
docker compose up -d db chrome_driver
docker compose run --rm --no-deps parsers python manage.py migrate --noinput
docker compose run --rm --no-deps parsers python manage.py setup_local
docker compose up -d bot parsers
```

Повторный запуск: `docker compose up -d db chrome_driver bot parsers`. Остановка с сохранением базы: `docker compose stop bot parsers chrome_driver db`. `setup_local` создаёт три строки Parser; существующие отключённые строки не включает автоматически.

Для сайта:

```powershell
docker compose --profile web build web
docker compose --profile web up -d web
```

Адрес `http://localhost:8000`; остановить сайт — `docker compose --profile web stop web`. Стартовая лента читает имеющиеся объявления без нового поиска. Для нового сбора нужен процесс parsers.

Маршруты Telegram и источников независимы. Karing оставляют включённым, если используется его proxy. Настройка собственного RU VLESS через `scripts/configure-avito-route.ps1 -Node msk -ProxyUrl "http://PROXY_HOST:PROXY_PORT" -NoRestart` является опциональной и требует существующего узла. Профиль avito-proxy включается только при подготовленном приватном JSON; [NETWORK.md](NETWORK.md).

При собственном запуске состояние видно через `docker compose ps` и `docker compose logs --tail 50 bot parsers`. Не публикуйте секреты/персональные идентификаторы из логов. Тестовая команда и требования к PostgreSQL — [DEVELOPMENT.md](DEVELOPMENT.md#6-тестирование); тесты в документирующей работе не запускались.

В Telegram: `/start`, «Найти машину», например `bmw 3 2018`. Выдача открывает одну карточку; «Списком» — десять строк с пагинацией. «Следить» сохраняет запрос с частотой; управление — «Отслеживание». `/status` показывает состояние площадок. Доступность источников этим документом не подтверждается.
