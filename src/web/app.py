"""Local web app for expert-case matching: pick or upload a CV, watch the pipeline, read the results.

Runs on 127.0.0.1 only. The LLM key stays in the server's environment and never reaches a page.
"""
import json
import re
import tempfile
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape

from src.core.config import ConfigError
from src.llm.config import LLMSettings
from src.matching.report import render
from src.matching.run import LOCAL_LIMITS, ROOT, case_status
from src.store.pipeline import PipelineStore
from src.web.jobs import RUN_ID, Jobs

MAX_UPLOAD = 10 * 1024 * 1024
STEPS = (("resume", "1. Resume text"), ("cv", "2. CV extraction"), ("search", "3. Case search"),
         ("screen", "4. Screening"), ("cases", "5. Case profiles"), ("match", "6. Matching"))
PROMPT_OF = {"cv": "expert_cv_v1", "search": "case_search_v1", "screen": "case_screen_v1",
             "cases": "case_profile_v1", "match": "case_match_v1"}


def pretty(raw):
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return raw


def inspection(step, trace, expert):
    """The model calls of one step, each with its input, raw output and what code kept of it."""
    calls = [{**t, "pretty": pretty(t["output"])} for t in trace if t["step"] == PROMPT_OF.get(step)]
    if not expert:
        return calls
    by_name = {r["case"]["name"]: r for r in expert["results"]}
    for call in calls:
        call["result"] = by_name.get(call["label"])
    return calls


def create_app(env, data_dir=ROOT / "data" / "matching", cases_file=ROOT / "eval" / "matching_cases.json",
               limit=160000):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
    samples_dir = data_dir / "cvs"
    samples_dir.mkdir(parents=True, exist_ok=True)
    cases = json.loads(cases_file.read_text(encoding="utf-8"))
    try:
        settings, config_error = LLMSettings.from_env(env), None
    except ConfigError as exc:
        settings, config_error = None, str(exc)
    jobs = Jobs(data_dir / "runs", settings, cases, ROOT, limit, data_dir, env)
    has_token = bool((env.get("COURTLISTENER_TOKEN") or "").strip())

    def samples():
        return sorted(p.name for p in samples_dir.glob("*.pdf"))

    def requests_today():
        """CourtListener requests made by local matching runs in the last 24 hours (their own ledger)."""
        path = data_dir / "courtlistener.db"
        if not path.exists():
            return 0
        store = PipelineStore(str(path))
        try:
            return store.execute("SELECT COUNT(*) AS n FROM api_requests WHERE api='courtlistener' AND attempted_at>?",
                                 (time.time() - 86400,)).fetchone()["n"]
        finally:
            store.close()

    def checked(run_id):
        if not re.fullmatch(RUN_ID, run_id) or jobs.status(run_id) is None:
            abort(404)
        return jobs.status(run_id)

    @app.get("/")
    def index():
        pool = [{**c, "status": case_status(c, ROOT)} for c in cases]
        return render_template("index.html", samples=samples(), cases=pool, history=jobs.history(),
                               busy=jobs.busy(), config_error=config_error, error=request.args.get("error"),
                               model=settings and settings.model, has_token=has_token, used=requests_today(),
                               cap=LOCAL_LIMITS.per_day, keep=jobs.keep)

    @app.post("/runs")
    def start():
        if config_error:
            return redirect(url_for("index", error="LLM settings are missing; start the server with --dotenv."))
        upload, sample = request.files.get("cv"), request.form.get("sample", "")
        with tempfile.TemporaryDirectory() as directory:
            if upload and upload.filename:
                path = Path(directory) / "cv.pdf"
                upload.save(path)
                if path.read_bytes()[:1024].find(b"%PDF-") < 0:
                    return redirect(url_for("index", error="The uploaded file is not a PDF."))
                name = Path(upload.filename).name
            elif sample in samples():
                path, name = samples_dir / sample, sample
            else:
                return redirect(url_for("index", error="Upload a CV PDF or choose a sample CV."))
            run_id = jobs.start(path, name, find=has_token and request.form.get("find") == "on")
        if run_id is None:
            return redirect(url_for("index", error="Another run is in progress; wait for it to finish."))
        return redirect(url_for("progress", run_id=run_id))

    @app.get("/runs/<run_id>")
    def progress(run_id):
        status = checked(run_id)
        if status["state"] == "done":
            return redirect(url_for("result", run_id=run_id))
        return render_template("progress.html", run_id=run_id, status=status)

    @app.get("/runs/<run_id>/status")
    def status(run_id):
        return jsonify(checked(run_id))

    @app.get("/runs/<run_id>/result")
    def result(run_id):
        status = checked(run_id)
        experts = jobs.result(run_id)
        if experts is None:
            return redirect(url_for("progress", run_id=run_id))
        return render(experts, status["finished"], status["model"], tabs(run_id, status, "result"))

    def tabs(run_id, status, active):
        """Top navigation shared by the results page and the step inspection pages."""
        links = [("result", "Results", url_for("result", run_id=run_id))]
        links += [(key, title, url_for("inspect", run_id=run_id, step=key)) for key, title in STEPS]
        items = "".join(f"<a class='tab{' on' if key == active else ''}' href='{href}'>{title}</a>" for key, title, href in links)
        return Markup(f"<nav><a href='{url_for('index')}'>← New run</a><a href='{url_for('result_json', run_id=run_id)}'>"
                      f"Download JSON</a><span class='muted'>CV: {escape(status['cv'])} · run {run_id}</span></nav>"
                      f"<nav class='tabs'>{items}</nav>")

    @app.get("/runs/<run_id>/inspect/<step>")
    def inspect(run_id, step):
        status = checked(run_id)
        if step not in dict(STEPS):
            abort(404)
        trace, experts = jobs.trace(run_id), jobs.result(run_id) or []
        expert = experts[0] if experts else None
        return render_template("inspect.html", step=step, title=dict(STEPS)[step], tabs=tabs(run_id, status, step),
                               trace=trace, calls=inspection(step, trace or [], expert), expert=expert, status=status)

    @app.get("/runs/<run_id>/result.json")
    def result_json(run_id):
        checked(run_id)
        path = jobs.folder(run_id) / "result.json"
        if not path.exists():
            abort(404)
        return Response(path.read_bytes(), mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename=matching-{run_id}.json"})

    @app.errorhandler(413)
    def too_large(_):
        return redirect(url_for("index", error="The CV is larger than 10 MB."))

    return app
