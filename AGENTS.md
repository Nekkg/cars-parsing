# Scope of this project

- Focus development and verification on the Telegram bot and its shared collectors/data services.
- The legacy website is frozen at the user's explicit request. Do not edit its views,
  templates, forms, URLs, static assets or frontend. Do not fix website-only failures.
- A possible future React Telegram Web App is deferred; do not start that work without
  a new user request. Shared backend changes needed by the bot remain in scope.
- User-facing language uses «Уведомления», not «Мониторинг». Preserve legacy commands
  as compatibility aliases where useful.
- Do not reconnect or push to the original BottecRu GitHub repository. The user will
  provide their own GitHub destination later. Preserve upstream license/attribution.
