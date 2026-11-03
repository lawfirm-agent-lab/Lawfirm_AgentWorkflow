"""Cloud copy of saved CVs and daily checks, in the pipeline's database (Supabase PostgreSQL).

The scheduled job (.github/workflows/daily-matching.yml) runs `python -m src.web.daily --cloud`:
it copies the saved CVs and earlier daily checks from the database into its empty run folder, runs
each CV's daily check, and saves every new check back. The local web app started with --cloud does
the reverse: it copies the cloud checks into data/matching/runs, so they show on the "New cases by
day" page, and saves CVs uploaded with "save this CV" to the cloud too.

python -m src.matching.cloud --dotenv upload data/matching/cvs/Jane_Doe.pdf   save a CV for daily checks
python -m src.matching.cloud --dotenv list                                    saved CVs and recent checks
python -m src.matching.cloud --dotenv pull                                    copy cloud checks to this computer
python -m src.matching.cloud --dotenv new-key --label advisor                 make a link to the results website
python -m src.matching.cloud --dotenv keys | revoke-key advisor               list or turn off website links
python -m src.matching.cloud --dotenv add-emails                              add lawyers' addresses to older checks
python -m src.matching.cloud --dotenv set-dispatch-token                      let uploads start cloud runs (Vault)

The results website (site/, on GitHub Pages) reads the daily checks through database functions
that check an access key (migration 007). Only the key's SHA-256 is stored; new-key prints the
link with the key once, and revoke-key turns every key with that label off.

Each command reads DATABASE_URL (TLS required) and never prints it; --sqlite PATH uses a local
file instead, for trying it out.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from src.core.config import ConfigError, secure_database_url
from src.matching import profiles
from src.store.db import StoreError, now
from src.store.pipeline import PipelineStore

SITE_URL = "https://ctrterry.github.io/Lawfirm_AgentWorkflow/"
RUN_FILES = (("status_json", "status.json"), ("result_json", "result.json"), ("trace_json", "trace.json"))


def open_store(env, sqlite=None):
    """The pipeline database; creating it applies the migrations, including the matching tables."""
    if sqlite:
        return PipelineStore(str(sqlite))
    return PipelineStore(database_url=secure_database_url(env.get("DATABASE_URL")))


class CloudRuns:
    """store: one open store, for a single-threaded command. connect: opens a store per call, for
    the web server (several threads, and a long idle time that can drop a kept connection)."""

    def __init__(self, store=None, connect=None):
        self.store, self.connect = store, connect
        self.lock = threading.Lock()

    @contextmanager
    def session(self):
        if self.connect is None:
            with self.lock:
                yield self.store
            return
        store = self.connect()
        try:
            yield store
        finally:
            store.close()

    def save_cv(self, name, data):
        with self.session() as store, store.transaction():
            store.execute(
                "INSERT INTO matching_cvs VALUES (?,?,?,1,?) ON CONFLICT (name) DO UPDATE SET "
                "pdf_base64=excluded.pdf_base64, sha256=excluded.sha256, active=1, saved_at=excluded.saved_at",
                (name, base64.b64encode(data).decode("ascii"), hashlib.sha256(data).hexdigest(), now()))

    def cvs(self):
        """Active saved CVs as (name, PDF bytes), by name."""
        with self.session() as store:
            rows = store.execute("SELECT name, pdf_base64 FROM matching_cvs WHERE active=1 ORDER BY name").fetchall()
        return [(r["name"], base64.b64decode(r["pdf_base64"])) for r in rows]

    def write_cvs(self, directory):
        """Copy the saved CVs into directory; returns their file names."""
        directory.mkdir(parents=True, exist_ok=True)
        names = []
        for name, data in self.cvs():
            if Path(name).name != name:  # Names come from the database: never write outside directory.
                continue
            (directory / name).write_bytes(data)
            names.append(name)
        return names

    def save_run(self, folder):
        """Save a finished run folder (status, result and trace). Saving it again replaces it."""
        files = {}
        for column, file_name in RUN_FILES:
            path = folder / file_name
            files[column] = path.read_text(encoding="utf-8") if path.exists() else None
        status = json.loads(files["status_json"])
        with self.session() as store, store.transaction():
            store.execute(
                "INSERT INTO matching_runs VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT (run_id) DO UPDATE SET "
                "state=excluded.state, finished_at=excluded.finished_at, status_json=excluded.status_json, "
                "result_json=excluded.result_json, trace_json=excluded.trace_json, saved_at=excluded.saved_at",
                (folder.name, status["cv"], status["state"], status["started"], status.get("finished"),
                 files["status_json"], files["result_json"], files["trace_json"], now()))

    def new_key(self, label):
        """Make a website access key; returns it. Only its hash is saved."""
        key = secrets.token_urlsafe(32)
        with self.session() as store, store.transaction():
            store.execute("INSERT INTO matching_access_keys VALUES (?,?,1,?)",
                          (hashlib.sha256(key.encode()).hexdigest(), label, now()))
        return key

    def keys(self):
        with self.session() as store:
            return store.execute("SELECT label, active, created_at FROM matching_access_keys "
                                 "ORDER BY created_at").fetchall()

    def revoke_keys(self, label):
        """Turn off every key with this label; returns how many were active."""
        with self.session() as store, store.transaction():
            return store.execute("UPDATE matching_access_keys SET active=0 WHERE label=? AND active=1",
                                 (label,)).rowcount

    def add_emails(self, fetch_pages):
        """Add lawyers' email addresses (profiles.add_emails) to saved checks made before they were
        found. fetch_pages(document_url) returns the document's page texts, or None to skip it.
        Returns how many checks changed."""
        with self.session() as store:
            rows = store.execute("SELECT run_id, result_json FROM matching_runs WHERE result_json IS NOT NULL").fetchall()
        changed = 0
        for row in rows:
            result, updated = json.loads(row["result_json"]), False
            for expert in result:
                for item in expert.get("results") or []:
                    profile, url = item.get("profile"), item["case"].get("document_url")
                    if not profile or "emails" in profile or not url:
                        continue
                    pages = fetch_pages(url)
                    if pages is not None:
                        item["profile"], updated = profiles.add_emails(profile, pages), True
            if updated:
                with self.session() as store, store.transaction():
                    store.execute("UPDATE matching_runs SET result_json=? WHERE run_id=?",
                                  (json.dumps(result, ensure_ascii=False, indent=1), row["run_id"]))
                changed += 1
        return changed

    def request(self, request_id):
        """An upload from the website (migration 008), or None."""
        with self.session() as store:
            return store.execute("SELECT * FROM matching_requests WHERE request_id=?", (request_id,)).fetchone()

    def update_request(self, request_id, **fields):
        """Set state, progress, run_id, error or pdf_base64 on a request (updated_at too)."""
        names = sorted(fields)
        assert set(names) <= {"state", "progress", "run_id", "error", "pdf_base64"}
        with self.session() as store, store.transaction():
            store.execute(f"UPDATE matching_requests SET {', '.join(f'{n}=?' for n in names)}, updated_at=? WHERE request_id=?",
                          (*[fields[n] for n in names], now(), request_id))

    def set_dispatch_token(self, token):
        """Keep the GitHub token that starts runs for uploads in Supabase Vault (PostgreSQL only)."""
        with self.session() as store, store.transaction():
            row = store.execute("SELECT id FROM vault.secrets WHERE name='matching_github_token'").fetchone()
            if row:
                store.execute("SELECT vault.update_secret(?, ?)", (row["id"], token))
            else:
                store.execute("SELECT vault.create_secret(?, 'matching_github_token')", (token,))

    def runs(self, limit=20):
        with self.session() as store:
            return store.execute("SELECT run_id, cv_name, state, started_at, finished_at FROM matching_runs "
                                 "ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()

    def pull(self, root):
        """Copy cloud runs that root does not have yet; returns how many were copied."""
        root.mkdir(parents=True, exist_ok=True)
        have = {p.name for p in root.iterdir() if (p / "status.json").exists()}
        copied = 0
        with self.session() as store:
            ids = [r["run_id"] for r in store.execute("SELECT run_id FROM matching_runs").fetchall()]
            for run_id in ids:
                if run_id in have or Path(run_id).name != run_id:
                    continue
                row = store.execute("SELECT * FROM matching_runs WHERE run_id=?", (run_id,)).fetchone()
                folder = root / run_id
                folder.mkdir(exist_ok=True)
                # status.json last: a folder counts as a run only once it is complete.
                for column, file_name in RUN_FILES[::-1]:
                    if row[column] is not None:
                        (folder / file_name).write_text(row[column], encoding="utf-8")
                copied += 1
        return copied


def document_pages():
    """fetch_pages for add_emails: complaint PDFs from CourtListener's public storage (no API
    request, no token), extracted like a case read; each document fetched once."""
    import tempfile

    import requests

    from src.ingest.pdf import ExtractionError, extract_pdf
    from src.llm.document import pages_from_extraction
    from src.matching.run import CASE_PAGES, STORAGE

    seen = {}

    def fetch(url):
        if not url.startswith(STORAGE):
            return None
        if url not in seen:
            try:
                response = requests.get(url, timeout=60)
                response.raise_for_status()
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "document.pdf"
                    path.write_bytes(response.content)
                    seen[url] = profiles.clean(pages_from_extraction(extract_pdf(path, layout=False, first_pages=CASE_PAGES)))
            except (OSError, ExtractionError):
                seen[url] = None
        return seen[url]

    return fetch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dotenv", action="store_true", help="Load DATABASE_URL from the local .env")
    parser.add_argument("--sqlite", type=Path, help="Use a local SQLite file instead of DATABASE_URL")
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload", help="Save a CV PDF for daily checks")
    upload.add_argument("pdf", type=Path)
    commands.add_parser("list", help="Saved CVs and recent daily checks")
    commands.add_parser("pull", help="Copy cloud daily checks into data/matching/runs")
    new_key = commands.add_parser("new-key", help="Make a link to the results website (shown once)")
    new_key.add_argument("--label", required=True, help="Who the link is for, e.g. advisor")
    commands.add_parser("keys", help="List website links (labels only)")
    revoke = commands.add_parser("revoke-key", help="Turn off the website links with this label")
    revoke.add_argument("label")
    commands.add_parser("add-emails", help="Add lawyers' email addresses to checks saved before they were found")
    commands.add_parser("set-dispatch-token", help="Store GITHUB_DISPATCH_TOKEN in Supabase Vault (never printed)")
    args = parser.parse_args(argv)
    from src.matching.run import ROOT
    if args.dotenv:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    try:
        store = open_store(os.environ, args.sqlite)
    except (ConfigError, StoreError) as exc:
        print(f"Database: {exc}")
        return 1
    cloud = CloudRuns(store)
    try:
        if args.command == "upload":
            data = args.pdf.read_bytes()
            if data[:1024].find(b"%PDF-") < 0:
                print(f"{args.pdf} is not a PDF")
                return 1
            cloud.save_cv(args.pdf.name, data)
            print(f"Saved {args.pdf.name} ({len(data) // 1024} KB); daily checks will include it.")
        elif args.command == "list":
            for name, data in cloud.cvs():
                print(f"CV  {name} ({len(data) // 1024} KB)")
            for r in cloud.runs():
                print(f"run {r['run_id']}  {r['cv_name']}  {r['state']}  {r['started_at']}")
        elif args.command == "new-key":
            key = cloud.new_key(args.label)
            print(f"Link for {args.label} (shown only now; anyone with it can read the results):")
            print(f"{SITE_URL}#key={key}")
        elif args.command == "keys":
            for r in cloud.keys():
                print(f"{r['label']}  {'active' if r['active'] else 'revoked'}  {r['created_at']}")
        elif args.command == "add-emails":
            print(f"Added lawyers' addresses to {cloud.add_emails(document_pages())} saved checks.")
        elif args.command == "set-dispatch-token":
            token = os.environ.get("GITHUB_DISPATCH_TOKEN", "").strip()
            if not token:
                print("Set GITHUB_DISPATCH_TOKEN in .env first.")
                return 1
            cloud.set_dispatch_token(token)
            print("Stored the GitHub token in Supabase Vault as matching_github_token.")
        elif args.command == "revoke-key":
            print(f"Turned off {cloud.revoke_keys(args.label)} link(s) labelled {args.label}.")
        else:
            print(f"Copied {cloud.pull(ROOT / 'data' / 'matching' / 'runs')} new daily checks from the cloud.")
    except StoreError as exc:
        print(f"Database: {exc}")
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
