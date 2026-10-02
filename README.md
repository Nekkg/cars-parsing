# Cars Parser

Локальный Telegram-бот для поиска автомобилей на Auto.ru, Дроме и Avito.
Фильтры, объявления и состояние поиска хранятся в PostgreSQL; бот показывает
по три машины с фотографиями и поддерживает отдельные уведомления.
На последней проверке Avito начал сохранять реальные объявления.
Фотографии Avito требуют отдельной доработки; доступ может снова ограничиваться.

## Структура

```text
docs/                          инструкции и история проекта
references/                    локальные справочные материалы, вне Git
reference-parser-daa/          приложение и Docker Compose
  bot/                         Telegram: команды, кнопки, отправка
  ads/                         объявления, каталог и поиск
  parsers/                     сборщики; collector.py — точка запуска
  core/                        Django, сетевые маршруты и рабочие пути
  users/, editing/             пользователи и тексты бота
  tests/{ads,bot,parsers}/      тесты по подсистемам
  scripts/diagnostics/         ручная проверка сети и источников
  scripts/legacy/              прежние вспомогательные команды
  runtime/                     логи, сессии, снимки; вне Git
  templates/, static/          замороженный веб-интерфейс
```

Имя каталога приложения сохранено, чтобы не менять существующие Docker-тома.
Настройки и секреты находятся в его локальном `.env` и не публикуются.

## Запуск и проверки

Нужен запущенный Docker Desktop. Команды из корня репозитория:

```powershell
cd reference-parser-daa
docker compose up -d
docker compose logs --tail 50 bot parsers
docker compose exec parsers python manage.py test tests --noinput
docker compose exec parsers python -m scripts.diagnostics.check_network
```

Для остановки с сохранением данных: `docker compose stop`.
Первый запуск и миграции описаны в [docs/LOCAL_RUN.md](docs/LOCAL_RUN.md).
Раздельные маршруты — в [docs/NETWORK.md](docs/NETWORK.md),
проверка Avito — в [docs/AVITO_BROWSER_SETUP.md](docs/AVITO_BROWSER_SETUP.md).

Прокси только для Avito: `scripts/set-avito-proxy.ps1` в каталоге приложения.
Этот инструмент вводит пароль скрыто и переключает Avito в HTTP-режим.
Веб-интерфейс не развивается. Исходная лицензия сохранена в
[LICENSE](reference-parser-daa/LICENSE); ранние материалы — в `docs/history/`.
