"""Flask app: import survey CSV + JSON jump table, edit per household, export.

Key behaviours:

* Import streams the CSV: the file and the cleaned rows are never held in
  memory.  Rows are written in short transactions into an unpublished batch,
  and the batch is published only after every household and answer is stored.
  A failure leaves no half-visible batch; existing batches can still be
  edited while the import runs.
* Every answer is stored under the path (question-id sequence) that reached
  it, so two routes into the same question keep separate answers and the
  earlier route's answers reappear when the walk returns to it.
* Exports show each household's current path, its effective answers, and its
  first unanswered question.
* Saves happen in one transaction; if anything fails, both the web page and
  the download keep showing the previously saved answers.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, url_for)

import engine
import importer
import memlimit
import storage

# Cap the process address space when configured.  Tests import this module
# without SURVEY_MEMORY_LIMIT_MB set, so the runner is never capped.
memlimit.apply_address_space_limit()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.secret_key = os.environ.get("SURVEY_SECRET", "orchard-survey-dev")

DB_PATH = storage.db_path_from_env()

# Rows per import commit.  Small enough to keep each write lock short and the
# transaction small; large enough that 16k households are a couple of flushes.
IMPORT_CHUNK = 250


def get_db():
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    return storage.connect(DB_PATH)


@app.errorhandler(sqlite3.Error)
def database_unavailable(error):
    """A storage failure never exposes half-written answers.

    Saves run in a transaction, so the previously committed survey is still
    intact; both the pages and the CSV download keep serving it once storage
    is reachable again.
    """
    app.logger.exception("database error")
    return render_template(
        "error.html",
        title="数据库暂时不可用",
        message="本次操作没有保存，网页和下载都会继续使用上次成功保存的答卷。",
        detail=str(error)), 503


@app.errorhandler(413)
def too_large(error):
    flash("上传文件过大（上限 32 MB）", "error")
    return redirect(url_for("index"))


def load_batch_or_404(connection, batch_id):
    info = storage.get_batch(connection, batch_id)
    if info is None:
        abort(404)
    return info


def _render_spooled(template_name, **context):
    """Render a template into a spooled file and stream it back.

    ``SpooledTemporaryFile`` keeps small pages in memory and rolls larger
    ones onto disk, so a 16k-household listing (10 MB+ of HTML) is never held
    in the process heap.  Rendering happens inside the request, which keeps
    Flask's context handling simple; the response generator only copies the
    finished file out in chunks and needs no request context.
    """
    template = app.jinja_env.get_or_select_template(template_name)
    context = dict(context)
    app.update_template_context(context)

    buffer = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    try:
        for chunk in template.generate(context):
            buffer.write(chunk.encode("utf-8"))
        buffer.seek(0)
    except BaseException:
        buffer.close()
        raise

    def generate():
        try:
            while True:
                chunk = buffer.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            buffer.close()

    return Response(generate(), mimetype="text/html; charset=utf-8")


@app.get("/")
def index():
    connection = get_db()
    try:
        # Best-effort cleanup of imports abandoned by a hard crash; a live
        # import is younger than the threshold and is left alone.
        try:
            storage.discard_stale_imports(connection)
        except sqlite3.Error:
            app.logger.exception("stale import cleanup failed")
        batches = storage.list_batches(connection)
    finally:
        connection.close()
    return render_template("index.html", batches=batches)


@app.post("/upload")
def upload():
    csv_file = request.files.get("csv_file")
    json_file = request.files.get("json_file")
    if csv_file is None or not csv_file.filename:
        flash("请选择答卷 CSV 文件", "error")
        return redirect(url_for("index"))
    if json_file is None or not json_file.filename:
        flash("请选择 JSON 跳题表", "error")
        return redirect(url_for("index"))

    # The CSV is streamed twice (validation scan, then write) from a spooled
    # temporary file, never from a bytes object in memory.
    try:
        plan = importer.plan_import(csv_file.stream, json_file.read())
    except (importer.ImportError_, engine.DefinitionError) as error:
        flash("导入失败：%s" % error, "error")
        return redirect(url_for("index"))

    name = request.form.get("batch_name", "").strip() or \
        os.path.splitext(os.path.basename(csv_file.filename))[0]

    connection = get_db()
    batch_id = None
    imported = 0
    published = False
    try:
        batch_id = storage.begin_import(
            connection, name, plan.definition)
        viewed = plan.cleaned_rows()
        chunk = []
        try:
            for household_no, row_number, answers in viewed:
                chunk.append((household_no, row_number, answers))
                if len(chunk) >= IMPORT_CHUNK:
                    storage.write_households(connection, batch_id, chunk)
                    chunk = []
            if chunk:
                storage.write_households(connection, batch_id, chunk)
        finally:
            close = getattr(viewed, "close", None)
            if close is not None:
                close()

        imported = storage.count_households(connection, batch_id)
        if imported == 0:
            raise importer.ImportError_("CSV 中没有数据行")
        if not storage.publish_batch(connection, batch_id):
            raise RuntimeError("批次在导入期间被移除")
        published = True
    except BaseException as error:
        # The batch was never published, so it is invisible everywhere.
        # Remove whatever was written and let the original file be retried.
        try:
            if batch_id is not None:
                try:
                    storage.discard_batch(connection, batch_id)
                except sqlite3.Error:
                    app.logger.exception("could not discard failed import")
        finally:
            connection.close()
        if not isinstance(error, Exception):
            raise  # interrupted request: clean up, then let it propagate
        if isinstance(error, (importer.ImportError_, engine.DefinitionError)):
            flash("导入失败：%s" % error, "error")
        else:
            app.logger.exception("import failed")
            flash("导入失败：%s（已有批次不受影响，可重新上传原文件）"
                  % error, "error")
        return redirect(url_for("index"))
    finally:
        plan.close()
        if published:
            connection.close()

    flash("已导入 %d 户，%d 道题" % (imported, len(plan.definition)),
          "success")
    if plan.warnings:
        flash("；".join(dict.fromkeys(plan.warnings)), "warning")
    return redirect(url_for("batch", batch_id=batch_id))


@app.get("/batch/<int:batch_id>")
def batch(batch_id):
    connection = get_db()
    try:
        info = load_batch_or_404(connection, batch_id)
        definition = info["definition"]
        total = storage.count_households(connection, batch_id)

        def summaries():
            # Resolve one chunk of households at a time so a 16k-household
            # batch does not need every answer set in memory at once.
            for household, stored in storage.iter_households_with_answers(
                    connection, batch_id):
                steps = engine.walk_stored(definition, stored)
                missing = engine.first_missing(steps)
                yield {
                    "id": household["id"],
                    "household_no": household["household_no"],
                    "route": engine.route(steps),
                    "missing": missing["qid"] if missing else None,
                    "answered": sum(1 for step in steps if step["answered"]),
                }

        return _render_spooled(
            "batch.html", batch=info, households=summaries(),
            household_count=total, question_count=len(definition))
    finally:
        connection.close()


@app.get("/batch/<int:batch_id>/household/<int:household_id>")
def household(batch_id, household_id):
    connection = get_db()
    try:
        info = load_batch_or_404(connection, batch_id)
        record = storage.get_household(connection, batch_id, household_id)
        if record is None:
            abort(404)
        stored = storage.load_answers(connection, household_id)
    finally:
        connection.close()
    return _render_household(info, record, stored)


@app.post("/batch/<int:batch_id>/household/<int:household_id>")
def save_household(batch_id, household_id):
    connection = get_db()
    try:
        info = load_batch_or_404(connection, batch_id)
        record = storage.get_household(connection, batch_id, household_id)
        if record is None:
            abort(404)

        submitted = collect_submitted(request.form)
        stored = storage.load_answers(connection, household_id)
        try:
            changes, ignored = engine.plan_changes(
                info["definition"], stored, submitted)
            storage.save_answers(connection, household_id, changes)
        except engine.DefinitionError as error:
            flash("保存失败：%s（网页和下载仍显示上次保存的答卷）" % error,
                  "error")
            return redirect(url_for("household", batch_id=batch_id,
                                    household_id=household_id))
        except Exception as error:  # transaction rolled back: nothing changed
            app.logger.exception("save failed")
            flash("保存失败：%s（网页和下载仍显示上次保存的答卷）" % error,
                  "error")
            return redirect(url_for("household", batch_id=batch_id,
                                    household_id=household_id))

        current = storage.load_answers(connection, household_id)
    finally:
        connection.close()

    if ignored:
        flash("题 %s 不在当前路径上，未保存" % "、".join(dict.fromkeys(ignored)),
              "warning")
    saved = sum(1 for value in changes.values() if value is not None)
    flash("已保存 %d 处修改" % saved, "success")
    return _render_household(info, record, current)


def collect_submitted(form):
    """Turn the edit form into ``[(qid, prefix, value)]`` in page order."""
    entries = []
    index = 1
    while ("k_qid_%d" % index) in form:
        qid = form["k_qid_%d" % index]
        prefix = form.get("k_prefix_%d" % index, "[]")
        try:
            parsed = json.loads(prefix)
            prefix = tuple(str(item) for item in parsed)
        except (TypeError, ValueError):
            prefix = ()
        entries.append((qid, prefix, form.get("v_%d" % index, "")))
        index += 1
    return entries


def _render_household(info, record, stored):
    definition = info["definition"]
    steps = engine.walk_stored(definition, stored)
    return render_template(
        "household.html",
        batch=info,
        household=record,
        steps=steps,
        missing=engine.first_missing(steps),
        others=engine.other_path_answers(definition, stored, steps),
    )


@app.get("/batch/<int:batch_id>/export.csv")
def export_csv(batch_id):
    connection = get_db()
    try:
        info = load_batch_or_404(connection, batch_id)
    except BaseException:
        connection.close()
        raise
    definition = info["definition"]

    qids = [question["id"] for question in definition]
    titles = {question["id"]: question["title"] for question in definition}

    header = ["户号", "当前路径", "已答题数"]
    header += ["%s %s" % (qid, titles[qid]) for qid in qids]
    header += ["首个漏答题", "漏答题干"]

    def generate():
        # Stream the CSV row by row, with a small buffer, so the whole export
        # is never held in memory alongside the database connection.
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush():
            chunk = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return chunk

        writer.writerow(header)
        yield "﻿" + flush()
        try:
            for household, stored in storage.iter_households_with_answers(
                    connection, batch_id):
                steps = engine.walk_stored(definition, stored)
                values = {step["qid"]: step["value"] for step in steps
                          if step["answered"]}
                missing = engine.first_missing(steps)
                row = [household["household_no"],
                       " → ".join(engine.route(steps)), len(values)]
                row += [values.get(qid, "") for qid in qids]
                row += [missing["qid"] if missing else "",
                        missing["title"] if missing else ""]
                writer.writerow(row)
                yield flush()
        finally:
            connection.close()

    return Response(generate(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; "
                             "filename=survey_%d_export.csv" % batch_id})


@app.get("/batch/<int:batch_id>/definition.json")
def export_definition(batch_id):
    connection = get_db()
    info = load_batch_or_404(connection, batch_id)
    connection.close()
    return info["definition"]


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
            debug=os.environ.get("SURVEY_DEBUG") == "1")
