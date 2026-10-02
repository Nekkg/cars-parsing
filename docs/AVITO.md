# Avito collector

The bot uses its existing shared database, filter matcher and pagination. Only
the Avito transport/structured catalogue reader changes. It never starts a
second Telegram bot or sends a separate stream of notifications.

Reference reviewed: https://github.com/Duff89/parser_avito at
`91a5399f79bf035d3eb919d66ada00545571c2f1`. Our adapter is independently written;
the reference repository is not vendored or a nested Git dependency.
The reference uses curl_cffi, catalogue JSON and cookie/proxy providers. Its
current mobile API URL conversion depends on SPFA; that paid/external cookie
workflow is NOT enabled here. This adapter reads the public listing page's
embedded catalogue JSON and falls back to the existing HTML card reader.

## Local .env settings

```dotenv
AVITO_TRANSPORT=http
# Optional dedicated proxy for Avito; omitted = SOURCES_PROXY_URL.
# An explicitly empty value means direct access.
# AVITO_PROXY_URL=http://username:password@host:port
# Optional export of your own browser cookies, stored only locally.
# AVITO_COOKIES_FILE=runtime/diagnostics/avito_cookies.json
```

Cookies may be a browser-exported JSON list, a name/value object, or an object
with a `cookies` member. Only Avito-domain cookies are loaded from a list.
The file reloads when modified. Never commit or send this file or proxy passwords.
Cookies do not guarantee access when the IP itself is blocked.

The HTTP session uses the dedicated Avito route, at least five seconds between
requests, no automatic retry on HTTP 403/429/439, and the collector's existing
15-minute backoff. Telegram and other sources retain their own routes.
To use the Selenium/manual browser workflow instead set `AVITO_TRANSPORT=browser`.
Changing .env requires recreating the parsers service:

```powershell
docker --context desktop-linux compose up -d --no-deps --build parsers
```

The browser route also uses AVITO_PROXY_URL. HTTP cookie file loading applies
only to the HTTP transport. Source image downloading still uses SOURCES_PROXY_URL.

## Verification

```powershell
docker --context desktop-linux compose exec -T parsers python manage.py test tests.parsers.test_avito_client tests.ads.test_search tests.bot.test_notification_limits
```

On the current connection, the live test returned HTTP 429 and the page title
`Доступ ограничен: проблема с IP`. No live ads were obtained in that test.
The adapter tests use synthetic response data and do not establish live access.
An accessible connection/session remains necessary; changing parsers alone does
not resolve an IP restriction. No paid services have been activated.


# Прямое подключение Avito

Avito использует домашнее подключение через прямой локальный порт Karing:

```dotenv
AVITO_TRANSPORT=http
AVITO_PROXY_URL=http://host.docker.internal:3065
```

Порт `3065` направлен в `direct_out`, порт Telegram `3066` использует VPN.
Проверка подтвердила совпадение IP прямого порта Karing с прямым выходом Windows.
При этом прямое соединение из Docker без указания прокси использовало другой IP,
поэтому для Avito явно задан прямой порт Karing. Karing должен оставаться запущенным.

На момент проверки 2 октября 2026 года Avito через прямой выход Windows вернул
HTTP 429 и страницу «Доступ ограничен: проблема с IP». Объявления не получены.
Сборщик сохраняет статус ограничения и делает паузу вместо непрерывных повторов.

Сторонний API-сервис отключён и его код удалён. Новые сервисы не используются.
