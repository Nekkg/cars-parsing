# Локальный запуск

Команды выполняются из `reference-parser-daa/`. Нужны Docker Desktop и локальный
`.env`. Для Telegram с VPN должен работать указанный в `.env` прокси Karing.

Первый запуск:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\configure-avito-route.ps1 -Node msk -NoRestart
docker compose up -d db chrome_driver
docker compose build bot parsers
docker compose run --rm --no-deps parsers python manage.py migrate
docker compose run --rm --no-deps parsers python manage.py setup_local
docker compose up -d bot parsers
```

Повторный запуск: `docker compose up -d`. Остановка с сохранением базы:
`docker compose stop`. Не запускайте `docker compose down -v`, если нужна база.

Проверки:

```powershell
docker compose ps
docker compose logs --tail 50 bot parsers
docker compose exec -T bot python manage.py test tests --noinput
```

В Telegram: `/start`, «Фильтры», затем запрос, например `bmw 3 2018`.
Выдача показывает три объявления; уведомления включаются отдельно. `/status`
показывает состояние площадок. Сбор продолжается в фоне.

[Avito](AVITO.md) · [Сетевые подключения](NETWORK.md).
Веб-сервис имеет профиль `web` и в обычном запуске не поднимается.