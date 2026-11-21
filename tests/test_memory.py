"""Memory-bounded import/export under a hard address-space limit.

These tests run the real Flask app in a subprocess with ``RLIMIT_AS`` set,
mirroring the deployment constraint (the process address space is capped).
The generated CSV is at the target scale: ~2.3 MB, 16k households, 45
questions.
"""

import os
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
LIMIT_MB = 192

SCRIPT = textwrap.dedent("""
    import io, json, os, resource, sys

    limit = __LIMIT__ * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    sys.path.insert(0, __APP__)
    os.environ["SURVEY_DB"] = __DB__
    os.environ["SURVEY_SECRET"] = "test"

    import main
    main.app.config.update(TESTING=True)

    HOUSEHOLDS = __HOUSEHOLDS__
    QUESTIONS = __QUESTIONS__

    def build_csv():
        header = "户号," + ",".join(
            "Q%d 题目%d" % (i + 1, i + 1) for i in range(QUESTIONS))
        lines = [header]
        for h in range(HOUSEHOLDS):
            cells = []
            for i in range(QUESTIONS):
                if i % 2 == 0:
                    cells.append("选项%d" % ((h + i) % 5))
                else:
                    cells.append(str((h + i) % 7))
            lines.append("H%05d," % h + ",".join(cells))
        return ("\\n".join(lines) + "\\n").encode("utf-8")

    def build_jumps():
        return json.dumps({
            "order": ["Q%d" % (i + 1) for i in range(QUESTIONS)],
            "types": {"Q%d" % (i + 1):
                      ("single" if i % 2 == 0 else "number")
                      for i in range(QUESTIONS)},
            "options": {"Q%d" % (i + 1):
                        ["选项%d" % k for k in range(5)]
                        for i in range(0, QUESTIONS, 2)},
            "jumps": {},
        }).encode("utf-8")

    csv_bytes = build_csv()
    jumps_bytes = build_jumps()
    print("csv_bytes=%d" % len(csv_bytes), flush=True)

    with main.app.test_client() as client:
        response = client.post("/upload", data={
            "csv_file": (io.BytesIO(csv_bytes), "big.csv"),
            "json_file": (io.BytesIO(jumps_bytes), "jumps.json"),
            "batch_name": "big",
        }, content_type="multipart/form-data")
        assert response.status_code == 302, response.status_code
        location = response.headers["Location"]
        page = client.get(location)
        assert page.status_code == 200
        html = page.get_data()
        assert ("H%05d" % (HOUSEHOLDS - 1)).encode() in html
        export = client.get(location + "/export.csv")
        assert export.status_code == 200
        rows = export.get_data().decode("utf-8-sig").splitlines()
        assert len(rows) == HOUSEHOLDS + 1, len(rows)
        print("rows=%d" % (len(rows) - 1), flush=True)
""")

DUPLICATE_SCRIPT = textwrap.dedent("""
    import io, json, os, resource, sys

    limit = __LIMIT__ * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    sys.path.insert(0, __APP__)
    os.environ["SURVEY_DB"] = __DB__
    os.environ["SURVEY_SECRET"] = "test"

    import main
    main.app.config.update(TESTING=True)
    main.IMPORT_CHUNK = 1  # force the duplicate across a chunk boundary

    rows = ["户号,Q1 来源,Q2 型号"]
    rows.append("H1,自购,喷药机")
    rows.append("H1,租赁,割草机")  # duplicate in a later chunk
    rows.append("H2,租赁,割草机")
    payload = ("\\n".join(rows) + "\\n").encode("utf-8")
    jumps = json.dumps({
        "order": ["Q1", "Q2"],
        "types": {"Q1": "single"},
        "options": {"Q1": ["自购", "租赁"]},
        "jumps": {"Q1": {"自购": "Q2"}},
    }).encode("utf-8")

    with main.app.test_client() as client:
        response = client.post("/upload", data={
            "csv_file": (io.BytesIO(payload), "answers.csv"),
            "json_file": (io.BytesIO(jumps), "jumps.json"),
        }, content_type="multipart/form-data")
        assert response.status_code == 302

        export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
        lines = [line for line in export.splitlines() if line.startswith("H1")]
        assert len(lines) == 1, lines
        assert "自购" in lines[0] and "租赁" not in lines[0], lines[0]
        print("ok", flush=True)
""")


def _fill(template, tmp_path, **values):
    values.setdefault("limit", LIMIT_MB)
    values.setdefault("app", repr(APP))
    values.setdefault("db", repr(str(tmp_path / "survey.sqlite3")))
    text = template
    for key, value in values.items():
        text = text.replace("__%s__" % key.upper(), str(value))
    return text


def run_limited(tmp_path, template, **values):
    path = tmp_path / "run.py"
    path.write_text(_fill(template, tmp_path, **values), encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = APP
    environment["MALLOC_ARENA_MAX"] = "1"
    # -E ignores the environment and -P does not prepend the script
    # directory, so nothing planted next to the temp database is imported.
    # The app directory is added explicitly inside the script instead.
    return subprocess.run([sys.executable, "-E", "-P", str(path)],
                          cwd=str(tmp_path), env=environment,
                          capture_output=True, text=True, timeout=300)


def test_import_and_export_stay_under_192_mib(tmp_path):
    result = run_limited(tmp_path, SCRIPT, households=16000, questions=45)
    assert result.returncode == 0, result.stderr[-4000:]
    assert "rows=16000" in result.stdout, result.stdout + result.stderr


def test_duplicate_across_chunks_keeps_first_under_limit(tmp_path):
    result = run_limited(tmp_path, DUPLICATE_SCRIPT)
    assert result.returncode == 0, result.stderr[-4000:]
    assert "ok" in result.stdout
