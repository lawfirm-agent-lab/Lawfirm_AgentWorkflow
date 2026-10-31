"""Matching runs in a background thread, one at a time. Each run is a folder under runs/:

cv.pdf        the uploaded or sample CV
status.json   state (queued, running, done, failed), finished steps, error, timing
result.json   the ranked results (when done)
trace.json    every step's input and model output (for the inspection pages)

A daily check is a run with "daily" set: it only searches complaints filed in the last `days`
days, skips cases earlier daily checks of the same CV already showed, and leaves out the fixed
case pool.

The status file is rewritten after every step, so the progress page and the run history
read it from disk and survive a server restart (an interrupted run shows as failed).
"""
import json
import secrets
import shutil
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from src.llm.client import LLMError
from src.matching.run import Model, make_search, run

RUN_ID = r"\d{8}-\d{6}-[0-9a-f]{6}"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


class Jobs:
    def __init__(self, root, settings, cases, base, limit, data_dir, env, keep=6):
        self.root, self.settings, self.cases, self.base, self.limit = root, settings, cases, base, limit
        self.data_dir, self.env, self.keep = data_dir, env, keep
        self.lock = threading.Lock()
        self.active = None
        root.mkdir(parents=True, exist_ok=True)

    def folder(self, run_id):
        return self.root / run_id

    def status(self, run_id):
        path = self.folder(run_id) / "status.json"
        if not path.exists():
            return None
        status = json.loads(path.read_text(encoding="utf-8"))
        if status["state"] in ("queued", "running") and run_id != self.active:
            status = {**status, "state": "failed", "error": "Interrupted (the server was restarted)."}
        return status

    def result(self, run_id):
        path = self.folder(run_id) / "result.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def history(self, limit=20):
        runs = sorted((p.name for p in self.root.iterdir() if (p / "status.json").exists()), reverse=True)
        return [{"id": r, **self.status(r)} for r in runs[:limit]]

    def busy(self):
        return self.active is not None

    def daily_runs(self):
        """Finished daily checks, newest first."""
        return [r for r in self.history(limit=200) if r.get("daily") and r["state"] == "done"]

    def shown(self, cv_name):
        """Docket URLs that earlier daily checks of this CV already found (screened or read)."""
        urls = set()
        for r in self.daily_runs():
            if r["cv"] == cv_name:
                for expert in self.result(r["id"]) or []:
                    urls.update(c["url"] for c in (expert.get("discovery") or {}).get("candidates") or [])
        return urls

    def start(self, cv_source, cv_name, find=False, days=None, wait=False):
        """Copy the CV into a new run folder and start the pipeline. Returns the run id or None if busy.

        days: a daily check of complaints filed in the last `days` days. wait: run in this thread."""
        daily = days is not None
        find = find or daily
        cases = [] if daily else self.cases
        with self.lock:
            if self.active:
                return None
            run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
            folder = self.folder(run_id)
            folder.mkdir()
            shutil.copyfile(cv_source, folder / "cv.pdf")
            # Estimate: CV, then (search plan, 3 searches, screening), then profile + match per case.
            total = 1 + (5 if find else 0) + 2 * (len(cases) + (self.keep if find else 0))
            write_json(folder / "status.json", {"state": "queued", "cv": cv_name, "model": self.settings.model,
                                                "find": find, "daily": daily, "days": days if daily else 365,
                                                "cases": len(cases), "total": total, "steps": [],
                                                "error": None, "started": now(), "finished": None})
            self.active = run_id
        if wait:
            self.work(run_id)
        else:
            threading.Thread(target=self.work, args=(run_id,), daemon=True).start()
        return run_id

    def work(self, run_id):
        folder = self.folder(run_id)
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))

        def log(item):
            status["steps"].append(item)
            if "total_hint" in item:  # Screening decided how many found cases will be read.
                status["total"] = len(status["steps"]) + 2 * (len(self.cases) + item["total_hint"])
            write_json(folder / "status.json", status)

        model = None
        try:
            status["state"] = "running"
            write_json(folder / "status.json", status)
            model = Model(self.settings, self.data_dir / "cache")
            daily = status.get("daily")
            search = make_search(self.env, self.data_dir, status.get("days", 365)) if status.get("find") else None
            experts = run(model, [folder / "cv.pdf"], [] if daily else self.cases, self.base, self.limit, log, search,
                          self.keep, self.data_dir / "pdfs", cv_name=status["cv"],
                          skip=self.shown(status["cv"]) if daily else ())
            write_json(folder / "result.json", experts)
            status.update(state="done", stats=model.stats)
        except LLMError as exc:
            status.update(state="failed", error=f"LLM call failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - any failure must reach the progress page
            status.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            (folder / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            if model is not None:  # Kept for failed runs too: the inspection pages show how far it got.
                write_json(folder / "trace.json", model.trace)
            status["finished"] = now()
            write_json(folder / "status.json", status)
            self.active = None

    def trace(self, run_id):
        path = self.folder(run_id) / "trace.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
