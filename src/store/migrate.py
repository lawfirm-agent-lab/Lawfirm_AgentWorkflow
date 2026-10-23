"""Apply versioned SQL migrations once, in order, inside one transaction."""
from pathlib import Path

from src.store.db import now

MIGRATIONS = Path(__file__).resolve().parents[2] / "sql" / "migrations"

# Supabase exposes the public schema through its Data API. RLS without policies denies
# those API roles while the owning application role keeps full access.
ENABLE_RLS = """
DO $$
DECLARE t record;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables
             WHERE schemaname = 'public' AND tableowner = current_user AND NOT rowsecurity
    LOOP
        EXECUTE 'ALTER TABLE public.' || quote_ident(t.tablename) || ' ENABLE ROW LEVEL SECURITY';
    END LOOP;
END $$
"""


def statements(text):
    lines = [line for line in text.splitlines() if not line.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def apply_migrations(store, directory=MIGRATIONS):
    """Return newly applied versions. Re-running is a no-op; existing rows are untouched."""
    files = sorted(Path(directory).glob("[0-9][0-9][0-9]_*.sql"))
    applied = []
    with store.transaction():
        if store.backend == "postgres":
            # Serialize concurrent first runs; released at commit.
            store.execute("SELECT pg_advisory_xact_lock(7313279903)")
        store.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        done = {row["version"] for row in store.execute("SELECT version FROM schema_migrations")}
        for path in files:
            if path.stem in done:
                continue
            for statement in statements(path.read_text(encoding="utf-8")):
                store.execute(statement)
            store.execute("INSERT INTO schema_migrations VALUES (?, ?)", (path.stem, now()))
            applied.append(path.stem)
        if store.backend == "postgres":
            store.execute(ENABLE_RLS)
    return applied
