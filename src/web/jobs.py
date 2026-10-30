"""Matching runs in a background thread, one at a time. Each run is a folder under runs/:

cv.pdf        the uploaded or sample CV
status.json   state (queued, running, done, failed), finished steps, error, timing
result.json   the ranked results (when done)

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
from src.matching.run import Model, run

RUN_ID = r"\d{8}-\d{6}-[0-9a-f]{6}"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)


class Jobs:
    def __init__(self, root, settings, cases, base, limit, cache_dir):
        self.root, self.settings, self.cases, self.base, self.limit = root, settings, cases, base, limit
        self.cache_dir = cache_dir
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

    def start(self, cv_source, cv_name):
        """Copy the CV into a new run folder and start the pipeline. Returns the run id or None if busy."""
        with self.lock:
            if self.active:
                return None
            run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
            folder = self.folder(run_id)
            folder.mkdir()
            shutil.copyfile(cv_source, folder / "cv.pdf")
            total = 2 * len(self.cases) + 1
            write_json(folder / "status.json", {"state": "queued", "cv": cv_name, "model": self.settings.model,
                                                "cases": len(self.cases), "total": total, "steps": [],
                                                "error": None, "started": now(), "finished": None})
            self.active = run_id
        threading.Thread(target=self.work, args=(run_id,), daemon=True).start()
        return run_id

    def work(self, run_id):
        folder = self.folder(run_id)
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))

        def log(item):
            status["steps"].append(item)
            write_json(folder / "status.json", status)

        try:
            status["state"] = "running"
            write_json(folder / "status.json", status)
            model = Model(self.settings, self.cache_dir)
            experts = run(model, [folder / "cv.pdf"], self.cases, self.base, self.limit, log)
            write_json(folder / "result.json", experts)
            status.update(state="done", stats=model.stats)
        except LLMError as exc:
            status.update(state="failed", error=f"LLM call failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - any failure must reach the progress page
            status.update(state="failed", error=f"{type(exc).__name__}: {exc}")
            (folder / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        finally:
            status["finished"] = now()
            write_json(folder / "status.json", status)
            self.active = None
