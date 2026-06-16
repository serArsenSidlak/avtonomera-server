# Автономера — server (bot + API)

Telegram bot + JSON API for the "Моніторинг Автономерів" service, running against a
Supabase Postgres database. The scraper stays off-server (residential IP). Secrets are
provided via `local/.env` at deploy time (never committed).
