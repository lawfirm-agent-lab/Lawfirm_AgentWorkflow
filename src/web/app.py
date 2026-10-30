"""Local web app for expert-case matching: pick or upload a CV, watch the pipeline, read the results.

Runs on 127.0.0.1 only. The LLM key stays in the server's environment and never reaches a page.
"""
import json
import re
import tempfile
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for
from markupsafe import escape

from src.core.config import ConfigError
from src.llm.config import LLMSettings
from src.matching.report import render
from src.matching.run import ROOT, case_status
from src.web.jobs import RUN_ID, Jobs

MAX_UPLOAD = 10 * 1024 * 1024


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
    jobs = Jobs(data_dir / "runs", settings, cases, ROOT, limit, data_dir / "cache")

    def samples():
        return sorted(p.name for p in samples_dir.glob("*.pdf"))

    def checked(run_id):
        if not re.fullmatch(RUN_ID, run_id) or jobs.status(run_id) is None:
            abort(404)
        return jobs.status(run_id)

    @app.get("/")
    def index():
        pool = [{**c, "status": case_status(c, ROOT)} for c in cases]
        return render_template("index.html", samples=samples(), cases=pool, history=jobs.history(),
                               busy=jobs.busy(), config_error=config_error, error=request.args.get("error"),
                               model=settings and settings.model)

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
            run_id = jobs.start(path, name)
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
        nav = (f"<nav><a href='{url_for('index')}'>← New run</a><a href='{url_for('result_json', run_id=run_id)}'>"
               f"Download JSON</a><span class='muted'>CV: {escape(status['cv'])} · run {run_id}</span></nav>")
        return render(experts, status["finished"], status["model"], nav)

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
