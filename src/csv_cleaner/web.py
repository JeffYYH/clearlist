"""HTML workflow and session-bound snapshot downloads."""

from __future__ import annotations

import io
import json
import math
import secrets
from importlib.resources import files

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import RequestEntityTooLarge

from .engine import (
    MAX_INPUT_BYTES,
    CleaningOptions,
    DataError,
    analyse,
    clean,
    formula_like,
    missing,
    parse_csv,
)
from .state import StoreFull

bp = Blueprint("web", __name__)
PAGE_SIZE = 50


def store():
    return current_app.extensions["jobs"]


def current_job():
    return store().get(session.get("job_id"))


def problem(message, status=400):
    return render_template("error.html", message=message), status


def page_of(items, name="page"):
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    try:
        number = int(request.args.get(name, "1"))
    except ValueError:
        number = 1
    number = min(max(1, number), pages)
    start = (number - 1) * PAGE_SIZE
    return {
        "items": items[start : start + PAGE_SIZE],
        "number": number,
        "pages": pages,
        "total": len(items),
        "start": start + 1,
        "end": min(start + PAGE_SIZE, len(items)),
    }


@bp.before_app_request
def prune():
    store().prune()


@bp.after_app_request
def headers(response):
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'none'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    return response


@bp.app_context_processor
def helpers():
    return {"is_missing": missing, "formula_like": formula_like}


@bp.get("/healthz")
def health():
    return {"status": "ok"}


@bp.get("/")
def index():
    return render_template("index.html", active_job=current_job() is not None)


@bp.get("/sample.csv")
def sample():
    content = files("csv_cleaner").joinpath("data/fictional_customers.csv").read_bytes()
    return send_file(
        io.BytesIO(content),
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="fictional_customers.csv",
    )


@bp.post("/upload")
def upload():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return problem("Choose a CSV file to upload.")
    if not uploaded.filename.lower().endswith(".csv"):
        return problem("Choose a .csv file exported with UTF-8 encoding and comma delimiters.")
    try:
        dataset = parse_csv(uploaded.read(MAX_INPUT_BYTES + 1))
        key = session.get("job_id") or secrets.token_urlsafe(32)
        store().put(key, dataset)
    except DataError as error:
        return problem(str(error))
    except StoreFull:
        return problem(
            "Four sessions are already active. Reset an unused session or try again after it expires.",
            503,
        )
    session["job_id"] = key
    return redirect(url_for("web.configure"), code=303)


def configuration(job, error=None, status=200):
    options = job.result.options if job.result else CleaningOptions()
    # On validation errors, keep entered text instead of silently resetting choices.
    if request.method == "POST":
        selected_trim = set(request.form.getlist("trim"))
        selected_fill = set(request.form.getlist("fill"))
        replacements = {
            str(i): request.form.get(f"replacement_{i}", "")
            for i in range(len(job.dataset.headers))
        }
        remove_empty = "remove_empty" in request.form
        remove_duplicates = "remove_duplicates" in request.form
    else:
        selected_trim = {str(i) for i in options.trim_columns}
        selected_fill = {str(i) for i, _ in options.replacements}
        replacements = {str(i): value for i, value in options.replacements}
        remove_empty = options.remove_empty
        remove_duplicates = options.remove_duplicates
    return render_template(
        "configure.html",
        dataset=job.dataset,
        report=analyse(job.dataset),
        original=page_of(tuple(enumerate(job.dataset.rows, start=1))),
        error=error,
        selected_trim=selected_trim,
        selected_fill=selected_fill,
        replacements=replacements,
        remove_empty=remove_empty,
        remove_duplicates=remove_duplicates,
    ), status


@bp.get("/configure")
def configure():
    job = current_job()
    if job is None:
        return problem(
            "This session has expired or was reset. Upload your CSV to start again.", 410
        )
    return configuration(job)


@bp.post("/preview")
def make_preview():
    job = current_job()
    if job is None:
        return problem(
            "This session has expired or was reset. Upload your CSV to start again.", 410
        )
    try:
        trim = tuple(int(value) for value in request.form.getlist("trim"))
        fill = tuple(
            (int(value), request.form.get(f"replacement_{value}", ""))
            for value in request.form.getlist("fill")
        )
        options = CleaningOptions(
            trim, "remove_empty" in request.form, fill, "remove_duplicates" in request.form
        )
        result = clean(job.dataset, options)
    except (ValueError, DataError) as error:
        message = (
            str(error)
            if isinstance(error, DataError)
            else "A selected column is invalid. Reload this page."
        )
        return configuration(job, message, 400)
    if not store().set_result(session["job_id"], job, result):
        return problem(
            "The upload changed while the preview was prepared. Review the current upload again.",
            409,
        )
    return redirect(url_for("web.preview", result_id=result.result_id), code=303)


def owned_result(result_id):
    job = current_job()
    if job is None or job.result is None or job.result.result_id != result_id:
        return None
    return job.result


def owned_snapshot(result_id):
    job = current_job()
    if job is None:
        return None
    result = job.result
    if result is None or result.result_id != result_id:
        return None
    return job, result


@bp.get("/preview/<result_id>")
def preview(result_id):
    snapshot = owned_snapshot(result_id)
    if snapshot is None:
        return problem(
            "This preview is unavailable or was replaced. Generate a new preview from your current upload.",
            410,
        )
    job, result = snapshot
    records = tuple(zip(result.source_rows, result.dataset.rows, strict=True))
    return render_template(
        "preview.html",
        result=result,
        original=job.dataset,
        output=page_of(records),
        changes=page_of(result.changes, "changes_page"),
        removed=page_of(result.removed, "removed_page"),
        summary=json.loads(result.summary_bytes),
    )


@bp.get("/download/<result_id>/<kind>")
def download(result_id, kind):
    result = owned_result(result_id)
    if result is None:
        return problem(
            "This result is unavailable or was replaced. Generate and review a new preview.", 410
        )
    if kind == "cleaned.csv":
        content, mimetype = result.csv_bytes, "text/csv; charset=utf-8"
    elif kind == "summary.json":
        content, mimetype = result.summary_bytes, "application/json"
    else:
        return problem("That download does not exist.", 404)
    response = send_file(
        io.BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=kind, max_age=0
    )
    response.headers["X-Result-ID"] = result.result_id
    return response


@bp.post("/reset")
def reset():
    store().discard(session.pop("job_id", None))
    return redirect(url_for("web.index"), code=303)


@bp.app_errorhandler(RequestEntityTooLarge)
def too_large(error):
    return problem(
        "The request is too large. Use a CSV of at most 2 MiB and shorter replacement text.", 413
    )


@bp.app_errorhandler(CSRFError)
def csrf_error(error):
    return problem("Your form has expired or is invalid. Reload the page and try again.", 400)


@bp.app_errorhandler(404)
def not_found(error):
    return problem("That page does not exist. Start with a CSV upload.", 404)
