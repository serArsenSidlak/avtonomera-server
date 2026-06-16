"""Storage dispatcher: SQLite (local MVP) or Postgres/Supabase, chosen by config.DB_BACKEND.

Every caller does ``from local import db`` and uses ``db.<func>``; this module re-exports the
selected backend's public API (functions, constants, is_pro, acquire, …) so the rest of the
app is backend-agnostic. Flip ``LOCAL_DB_BACKEND=postgres`` once the migration is verified.
"""
from local import config

if config.DB_BACKEND == "postgres":
    from local.db_pg import *  # noqa: F401,F403
else:
    from local.db_sqlite import *  # noqa: F401,F403
