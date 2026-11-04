"""Preview the results website on this computer with a copy of the cloud data, before pushing.

.venv/bin/python -m src.web.preview          then open the link it prints, with your own #key=...

Needs Docker. It starts a throwaway PostgreSQL and PostgREST (the API Supabase puts in front of
the database), applies the migrations up to the last one already in the cloud, copies the cloud
data in through a read-only connection (nothing in Supabase is changed), then applies this
branch's newer migrations to the copy, which shows what merging will do to the real data. The
site is served straight from site/, so an edit shows on the next page refresh; a change to the
SQL needs a restart. Ctrl+C stops it and removes the containers and the copy.

Not in the copy: the daily schedule's history and GitHub dispatches (pg_cron, pg_net), so an
upload made in the preview waits and is never run.

Only this computer can reach it: every port listens on 127.0.0.1. The site answers only requests
addressed to 127.0.0.1:8090 or localhost:8090 (a web page can point another name at 127.0.0.1 to
reach local servers) and refuses API calls sent by other websites. The API behind it takes only
requests signed with a secret made at each start, which only this server sends, and the database
password is also made at each start.
"""
import base64
import hashlib
import os
import hmac
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
NAME = "expert-match-preview"
DB_PORT, API_PORT, WEB_PORT = 55440, 55441, 8090
PASSWORD = secrets.token_urlsafe(18)
LOCAL = f"postgresql://postgres:{PASSWORD}@127.0.0.1:{DB_PORT}/preview"
JWT_SECRET = secrets.token_urlsafe(36)
HOSTS = {f"127.0.0.1:{WEB_PORT}", f"localhost:{WEB_PORT}"}
POSTGREST = "postgrest/postgrest:v12.2.3"
# Docker Desktop's own copy, for a terminal whose PATH lacks /usr/local/bin (some conda setups).
DOCKER_APP = "/Applications/Docker.app/Contents/Resources/bin/docker"
# Copied in this order; columns the copy has but the cloud does not yet get their defaults.
TABLES = ("matching_access_keys", "matching_cvs", "matching_runs", "matching_requests", "matching_feedback", "pipeline_runs")


def docker_command():
    found = shutil.which("docker") or (DOCKER_APP if os.access(DOCKER_APP, os.X_OK) else None)
    return found


def docker(*args, check=True):
    command = docker_command()
    # Its helpers (credentials, plugins) sit next to it; keep that folder on PATH for them.
    env = {**os.environ, "PATH": f"{os.path.dirname(command)}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run([command, *args], check=check, capture_output=True, text=True, env=env)


def token(role="anon"):
    """A JWT (HS256) PostgREST accepts as the website's role; only this process knows the secret."""
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=")
    body = encode({"alg": "HS256", "typ": "JWT"}) + b"." + encode({"role": role})
    signature = base64.urlsafe_b64encode(hmac.new(JWT_SECRET.encode(), body, hashlib.sha256).digest()).rstrip(b"=")
    return (body + b"." + signature).decode()


def stop():
    for container in (f"{NAME}-api", f"{NAME}-db"):
        docker("rm", "-f", container, check=False)
    docker("network", "rm", NAME, check=False)


def wait(test, what, seconds=60):
    for _ in range(seconds * 2):
        if test():
            return
        time.sleep(0.5)
    raise RuntimeError(f"{what} did not start")


def cloud_tables(cloud):
    """{table: rows} from the cloud, read-only. Tables the cloud does not have yet are skipped."""
    out = {}
    with cloud.cursor() as cur:
        for table in TABLES:
            cur.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",))
            if cur.fetchone()["t"]:
                cur.execute(f"SELECT * FROM {table}")
                out[table] = cur.fetchall()
    return out


def cloud_migrations(cloud):
    with cloud.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        return {row["version"] for row in cur.fetchall()}


def build_copy(cloud_url):
    import psycopg
    from psycopg.rows import dict_row

    from src.store.db import Store
    from src.store.migrate import MIGRATIONS, apply_migrations

    # default_transaction_read_only: this connection cannot change anything in the cloud.
    with psycopg.connect(cloud_url, row_factory=dict_row, connect_timeout=10,
                         options="-c default_transaction_read_only=on") as cloud:
        done, data = cloud_migrations(cloud), cloud_tables(cloud)
    store = Store(database_url=LOCAL)
    try:
        with tempfile.TemporaryDirectory() as directory:
            for path in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"):
                if path.stem in done:
                    shutil.copy(path, Path(directory) / path.name)
            apply_migrations(store, directory)
        for table, rows in data.items():
            local = {r["column_name"] for r in store.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,)).fetchall()}
            with store.transaction():
                for row in rows:
                    names = [k for k in row if k in local]
                    store.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                                  tuple(row[k] for k in names))
        before = store.execute("SELECT count(*) AS n FROM matching_feedback").fetchone()["n"] \
            if "matching_feedback" in data else 0
        newer = apply_migrations(store)
        after = store.execute("SELECT count(*) AS n FROM matching_feedback").fetchone()["n"]
    finally:
        store.close()
    return {table: len(rows) for table, rows in data.items()}, newer, before - after


class Handler(SimpleHTTPRequestHandler):
    """site/ from disk, a config.js pointing here, and /rest/v1/rpc/* passed on to PostgREST."""

    def log_message(self, *args):
        pass

    def allowed(self, api=False):
        """Refuse requests not addressed to this server, and API calls sent by other websites."""
        origin = self.headers.get("Origin")
        if self.headers.get("Host") not in HOSTS or (api and origin and origin.split("//")[-1] not in HOSTS):
            self.send_error(403)
            return False
        return True

    def do_HEAD(self):
        if self.allowed():
            super().do_HEAD()

    def do_GET(self):
        if not self.allowed():
            return
        if self.path.split("?")[0] == "/config.js":
            body = f'window.EXPERT_MATCH = {json.dumps({"url": f"http://127.0.0.1:{WEB_PORT}", "key": "preview"})};'.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # Edits show on the next refresh.
        super().end_headers()

    def do_POST(self):
        if not self.allowed(api=True):
            return
        if not self.path.startswith("/rest/v1/rpc/"):
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        request = urllib.request.Request(f"http://127.0.0.1:{API_PORT}/rpc/{self.path.rsplit('/', 1)[-1]}", data=body,
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {token()}"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as answer:
                status, data = answer.status, answer.read()
        except urllib.error.HTTPError as error:
            status, data = error.code, error.read()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def main():
    from dotenv import dotenv_values

    cloud_url = (dotenv_values(ROOT / ".env").get("DATABASE_URL") or "").strip()
    if not cloud_url:
        print("Set DATABASE_URL in .env (the cloud database to copy).")
        return 1
    if docker_command() is None:
        print("Docker is not installed (no docker command, and no Docker Desktop in /Applications).")
        return 1
    info = docker("info", "--format", "{{.ServerVersion}}", check=False)
    if info.returncode:
        reason = (info.stderr or info.stdout).strip().splitlines()
        print("Docker is not answering. Start Docker Desktop (wait until it says it is running) and try again.")
        print(f"  docker said: {reason[-1][:200] if reason else 'nothing'}")
        return 1
    stop()  # A preview left over from an earlier run.
    try:
        print("Starting a throwaway database...")
        docker("network", "create", NAME)
        docker("run", "-d", "--rm", "--name", f"{NAME}-db", "--network", NAME, "-e", f"POSTGRES_PASSWORD={PASSWORD}",
               "-e", "POSTGRES_DB=preview", "-p", f"127.0.0.1:{DB_PORT}:5432", "postgres:16")
        wait(lambda: not docker("exec", f"{NAME}-db", "pg_isready", "-U", "postgres", "-d", "preview", check=False).returncode
             and not docker("exec", f"{NAME}-db", "psql", "-U", "postgres", "-d", "preview", "-tAc", "SELECT 1", check=False).returncode,
             "PostgreSQL")
        # The website's role, as in Supabase, so the migrations' grants apply.
        docker("exec", f"{NAME}-db", "psql", "-U", "postgres", "-d", "preview", "-qc",
               "CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN; GRANT USAGE ON SCHEMA public TO anon;")
        print("Copying the cloud data (read-only)...")
        try:
            counts, newer, removed = build_copy(cloud_url)
        except Exception as exc:  # noqa: BLE001 - only the type: a database error can name the server
            raise RuntimeError(f"copying the data failed ({type(exc).__name__})") from None
        print("  copied " + ", ".join(f"{n} {t}" for t, n in counts.items()))
        if newer:
            print(f"  this branch's migrations, applied to the copy only: {', '.join(newer)}")
            if removed:
                print(f"  they removed {removed} feedback row(s), as they will in the cloud after merging")
        docker("run", "-d", "--rm", "--name", f"{NAME}-api", "--network", NAME, "-p", f"127.0.0.1:{API_PORT}:3000",
               # No anonymous role: a request without this process's signed token is refused.
               "-e", f"PGRST_DB_URI=postgres://postgres:{PASSWORD}@{NAME}-db:5432/preview", "-e", f"PGRST_JWT_SECRET={JWT_SECRET}",
               "-e", "PGRST_DB_SCHEMAS=public", POSTGREST)

        def api_up():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{API_PORT}/", timeout=2):
                    return True
            except urllib.error.HTTPError:
                return True  # It answers (refusing a request without a token): it is up.
            except (OSError, urllib.error.URLError):
                return False

        wait(api_up, "PostgREST")
        server = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), partial(Handler, directory=str(SITE)))
        print(f"\nPreview ready. Open your own link with this address in front:\n"
              f"  http://127.0.0.1:{WEB_PORT}/#key=<the key from your link>\n"
              "Edits to site/ show on refresh. Feedback, deletes and stops you make here change only the copy.\n"
              "Ctrl+C stops the preview and deletes the copy.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            server.server_close()
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        detail = exc.stderr.strip()[-300:] if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"Preview failed: {detail}")
        return 1
    finally:
        stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
