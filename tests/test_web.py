import html
import io
import json
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DB", str(tmp_path / "survey.sqlite3"))
    monkeypatch.setenv("SURVEY_SECRET", "test")
    for module in ("storage", "engine", "importer", "main"):
        sys.modules.pop(module, None)
    import main
    main.app.config.update(TESTING=True)
    with main.app.test_client() as test_client:
        yield test_client


CSV_TEXT = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,Q7 年费用合计（元）
H1,自购,喷药机,2,无,,,12000
H2,租赁,喷药机,2,有,按年整租,3,8000
H3,自购,,,,,,9000
H4,租赁,,,有,按季短租,,,
"""

JUMPS = {
    "order": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
    "titles": {
        "Q1": "设备来源", "Q2": "自购设备类型", "Q3": "自购台数",
        "Q4": "是否享受购置补贴", "Q5": "租赁方式", "Q6": "租赁台数",
        "Q7": "年费用合计（元）",
    },
    "types": {"Q1": "single", "Q2": "single", "Q4": "single",
              "Q5": "single", "Q3": "number", "Q6": "number",
              "Q7": "number"},
    "options": {"Q1": ["自购", "租赁"], "Q2": ["喷药机", "采摘平台"],
                "Q4": ["有", "无"], "Q5": ["按年整租", "按季短租"]},
    "jumps": {"Q1": {"自购": "Q2", "租赁": "Q5"},
              "Q2": {"采摘平台": "Q7"},
              "Q4": {"有": "Q7", "无": "Q7"}},
}


def upload(client, csv_text=CSV_TEXT, jumps=JUMPS):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "answers.csv"),
        "json_file": (io.BytesIO(json.dumps(jumps).encode("utf-8")),
                      "jumps.json"),
        "batch_name": "测试批次",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    return response


def form_fields(body):
    """(qid, prefix, value) for every question rendered on the edit page."""
    entries = []
    pattern = re.compile(
        r'name="k_qid_(\d+)"\s+value="([^"]*)">\s*'
        r'<input\s+type="hidden"\s+name="k_prefix_\1"\s+value="([^"]*)"',
        re.S)
    for index, qid, prefix in pattern.findall(body):
        tags = re.findall(r'<input[^>]*name="v_%s"[^>]*>' % index, body, re.S)
        chosen = next((tag for tag in tags if "checked" in tag),
                      tags[0] if tags else "")
        value_match = re.search(r'data-value="([^"]*)"', chosen)
        entries.append((html.unescape(qid), json.loads(html.unescape(prefix)),
                        html.unescape(value_match.group(1)) if value_match
                        else ""))
    return entries


def values(body):
    return {(qid, tuple(prefix)): value
            for qid, prefix, value in form_fields(body)}


def submit(client, entries, household=2):
    data = {}
    for position, (qid, prefix, value) in enumerate(entries, start=1):
        data["k_qid_%d" % position] = qid
        data["k_prefix_%d" % position] = json.dumps(list(prefix))
        data["v_%d" % position] = value
    return client.post("/batch/1/household/%d" % household, data=data,
                       follow_redirects=True).get_data(as_text=True)


def replace(entries, qid, prefix, value):
    return [(q, tuple(p), value if (q == qid and tuple(p) == tuple(prefix))
             else v) for q, p, v in entries]


SELF = ("Q1", "Q2", "Q3", "Q4")
RENT = ("Q1", "Q5", "Q6")


def test_import_and_list(client):
    body = upload(client).get_data(as_text=True)
    assert "测试批次" in body
    for household in ("H1", "H2", "H3", "H4"):
        assert household in body


def test_self_buy_path_and_shared_cost(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/1").get_data(as_text=True))
    assert [qid for qid, _, _ in fields] == ["Q1", "Q2", "Q3", "Q4", "Q7"]
    assert dict((q, v) for q, _, v in fields)["Q7"] == "12000"


def test_rental_path_skips_self_buy_questions(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    assert [qid for qid, _, _ in fields] == ["Q1", "Q5", "Q6", "Q7"]
    assert dict((q, v) for q, _, v in fields)["Q7"] == "8000"


def test_import_ignores_unreached_answers(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = text.splitlines()[0].split(",")
    h2 = next(line for line in text.splitlines() if line.startswith("H2"))
    cells = h2.split(",")
    # H2 is a rental household; its self-buy columns must not be imported.
    assert cells[header.index("Q2 自购设备类型")] == ""
    assert cells[header.index("Q3 自购台数")] == ""
    assert cells[header.index("Q4 是否享受购置补贴")] == ""
    assert cells[header.index("当前路径")] == "Q1 → Q5 → Q6 → Q7"
    assert cells[header.index("Q5 租赁方式")] == "按年整租"


def test_switch_branch_saves_separate_answers(client):
    upload(client)
    # H2 starts on the rental route.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "喷药机",
        "k_qid_3": "Q3", "k_prefix_3": '["Q1", "Q2"]', "v_3": "4",
        "k_qid_4": "Q4", "k_prefix_4": '["Q1", "Q2", "Q3"]', "v_4": "无",
        "k_qid_5": "Q7", "k_prefix_5": '["Q1", "Q2", "Q3", "Q4"]',
        "v_5": "15000",
    }, follow_redirects=True).get_data(as_text=True)

    assert "其他路径上保存的答案" in body
    current = values(body)
    assert current[("Q1", ())] == "自购"
    assert current[("Q7", SELF)] == "15000"
    # The rental path's answers are still stored.
    assert current[("Q5", ("Q1",))] if ("Q5", ("Q1",)) in current else True
    assert "按年整租" in body and "8000" in body

    # Switch back to rental: its own answers return, including its cost.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert current[("Q5", ("Q1",))] == "按年整租"
    assert current[("Q6", ("Q1", "Q5"))] == "3"
    assert current[("Q7", RENT)] == "8000"


def test_shared_cost_question_is_per_path(client):
    upload(client)
    # Change the rental household's cost.
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q7", RENT, "9000"))
    assert values(body)[("Q7", RENT)] == "9000"

    # Move to self-buy: the cost shows the self-buy value, not 9000.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "喷药机",
        "k_qid_3": "Q3", "k_prefix_3": '["Q1", "Q2"]', "v_3": "4",
        "k_qid_4": "Q4", "k_prefix_4": '["Q1", "Q2", "Q3"]', "v_4": "无",
        "k_qid_5": "Q7", "k_prefix_5": '["Q1", "Q2", "Q3", "Q4"]',
        "v_5": "",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert current[("Q7", SELF)] == ""
    assert "9000" in body  # still visible as another path's answer

    # Return to rental: 9000 is intact.
    body = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "租赁",
    }, follow_redirects=True).get_data(as_text=True)
    assert values(body)[("Q7", RENT)] == "9000"


def test_stale_fields_of_old_route_are_not_saved(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    # Keep every rental field but change the route: they are now stale.
    body = submit(client, [("Q1", (), "自购")] + fields[1:])
    current = values(body)
    assert [q for q, _ in current] == ["Q1", "Q2"]
    assert current[("Q2", ("Q1",))] == ""


def test_invalid_single_option_rejected_and_previous_answers_kept(client):
    upload(client)
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "丢弃",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert "按年整租" in export and "8000" in export


def test_export_lists_effective_answers_and_first_missing(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    lines = text.strip().splitlines()
    header = lines[0].split(",")
    assert header[0] == "户号"
    assert "当前路径" in header
    assert "Q7 年费用合计（元）" in header
    assert header[-2] == "首个漏答题"

    h2 = next(line for line in lines if line.startswith("H2"))
    cells = h2.split(",")
    assert cells[header.index("首个漏答题")] == ""
    assert cells[header.index("Q1 设备来源")] == "租赁"
    assert cells[header.index("Q5 租赁方式")] == "按年整租"
    assert cells[header.index("Q7 年费用合计（元）")] == "8000"


def test_missing_answer_is_reported(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = text.splitlines()[0].split(",")
    h3 = next(line for line in text.splitlines() if line.startswith("H3"))
    cells = h3.split(",")
    assert cells[header.index("Q1 设备来源")] == "自购"
    assert cells[header.index("首个漏答题")] == "Q2"
    assert cells[header.index("漏答题干")] == "自购设备类型"
    # Nothing after the first blank is imported, even though Q7 was filled.
    assert cells[header.index("Q7 年费用合计（元）")] == ""

    h4 = next(line for line in text.splitlines() if line.startswith("H4"))
    cells = h4.split(",")
    assert cells[header.index("Q5 租赁方式")] == "按季短租"
    assert cells[header.index("首个漏答题")] == "Q6"


def test_clearing_answer_stops_path(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q5", ("Q1",), ""))
    assert [qid for qid, _, _ in form_fields(body)] == ["Q1", "Q5"]


def test_jump_directly_to_shared_cost_question(client):
    upload(client)
    # 自购 + 采摘平台 jumps straight to Q7, skipping Q3/Q4.
    body = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "v_2": "采摘平台",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert [q for q, _ in current] == ["Q1", "Q2", "Q7"]
    assert current[("Q7", ("Q1", "Q2"))] == ""


def test_save_failure_keeps_last_saved_answers(client, monkeypatch):
    upload(client)
    import main
    original = main.storage.save_answers
    monkeypatch.setattr(main.storage, "save_answers",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            RuntimeError("disk full")))
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
    }, follow_redirects=True)
    assert "disk full" in response.get_data(as_text=True)

    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h2 = next(line for line in export.splitlines() if line.startswith("H2"))
    assert "租赁" in h2 and "8000" in h2
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert values(page)[("Q1", ())] == "租赁"
    assert values(page)[("Q7", RENT)] == "8000"
    monkeypatch.setattr(main.storage, "save_answers", original)


def test_database_error_keeps_download_on_last_saved(client, monkeypatch):
    upload(client)
    import main

    def explode(connection, household_id, changes):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main.storage, "save_answers", explode)
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "v_1": "自购",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h2 = next(line for line in export.splitlines() if line.startswith("H2"))
    assert "租赁" in h2 and "8000" in h2


def test_batch_route_returns_404_for_missing_batch(client):
    assert client.get("/batch/99").status_code == 404
    assert client.get("/batch/1/household/99").status_code == 404


def test_import_rejects_bad_json(client):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(CSV_TEXT.encode()), "answers.csv"),
        "json_file": (io.BytesIO(b"{not json"), "jumps.json"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert "导入失败" in response.get_data(as_text=True)


def test_import_rejects_bad_jump_target(client):
    jumps = dict(JUMPS, jumps={"Q1": {"租赁": "Q99"}})
    response = upload(client, jumps=jumps)
    assert "导入失败" in response.get_data(as_text=True)


def test_interrupted_import_leaves_no_partial_batch(client, monkeypatch):
    """A write failure mid-import must leave no batch and no orphan rows."""
    upload(client)  # seed a good batch 1
    import main
    original = main.storage.write_households
    calls = {"n": 0}

    def flaky(connection, batch_id, households):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk full")
        return original(connection, batch_id, households)

    monkeypatch.setattr(main.storage, "write_households", flaky)
    monkeypatch.setattr(main, "IMPORT_CHUNK", 2)
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(CSV_TEXT.encode()), "answers.csv"),
        "json_file": (io.BytesIO(json.dumps(JUMPS).encode()), "jumps.json"),
        "batch_name": "坏导入",
    }, content_type="multipart/form-data", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert "导入失败" in body
    assert "坏导入" not in body  # not listed as a batch

    # The existing batch is untouched and downloadable.
    assert client.get("/batch/1").status_code == 200
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert "H2" in export and "8000" in export
    # Nothing was left behind by the failed import.
    connection = main.get_db()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM batches").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM households").fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM answers").fetchone()[0] > 0
    finally:
        connection.close()


def test_importing_batch_not_visible_until_complete(client, monkeypatch):
    """During the write phase, the new batch is not on the index or viewable."""
    import main
    seen = {}
    original = main.storage.write_households

    def observe(connection, batch_id, households):
        if "first" not in seen:
            seen["first"] = True
            index = main.app.test_client().get("/").get_data(as_text=True)
            seen["index"] = index
            seen["status"] = main.app.test_client().get(
                "/batch/%d" % batch_id).status_code
        return original(connection, batch_id, households)

    monkeypatch.setattr(main.storage, "write_households", observe)
    monkeypatch.setattr(main, "IMPORT_CHUNK", 2)
    upload(client)
    assert seen["status"] == 404
    assert "answers" not in seen["index"].split("已有批次")[1]
    # And it becomes visible once the import completes.
    assert client.get("/batch/1").status_code == 200


def test_failed_import_can_be_retried(client, monkeypatch):
    import main
    original = main.storage.write_households
    monkeypatch.setattr(main.storage, "write_households",
                        lambda *a, **k: (_ for _ in ()).throw(
                            sqlite3.OperationalError("locked")))
    upload(client)
    assert client.get("/batch/1").status_code == 404
    assert "还没有批次" in client.get("/").get_data(as_text=True)
    monkeypatch.setattr(main.storage, "write_households", original)
    body = upload(client).get_data(as_text=True)
    assert "已导入 4 户" in body
    assert client.get("/batch/2").status_code == 200


def test_duplicate_household_across_chunks_keeps_first(client, monkeypatch):
    import main
    monkeypatch.setattr(main, "IMPORT_CHUNK", 1)
    csv_text = ("户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,"
                "Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,"
                "Q7 年费用合计（元）\n"
                "H1,自购,喷药机,2,无,,,12000\n"
                "H1,租赁,,,,按年整租,3,8000\n")
    upload(client, csv_text=csv_text)
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    lines = [line for line in export.splitlines() if line.startswith("H1")]
    assert len(lines) == 1
    assert "12000" in lines[0] and "按年整租" not in lines[0]


def test_interrupted_import_cleans_up_and_propagates(client, monkeypatch):
    """A BaseException mid-import removes the unpublished batch."""
    import main
    upload(client)  # batch 1 exists
    original = main.storage.write_households
    calls = {"n": 0}

    def interrupt(connection, batch_id, households):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt()
        return original(connection, batch_id, households)

    monkeypatch.setattr(main.storage, "write_households", interrupt)
    monkeypatch.setattr(main, "IMPORT_CHUNK", 2)
    with pytest.raises(KeyboardInterrupt):
        client.post("/upload", data={
            "csv_file": (io.BytesIO(CSV_TEXT.encode()), "answers.csv"),
            "json_file": (io.BytesIO(json.dumps(JUMPS).encode()), "jumps.json"),
        }, content_type="multipart/form-data")

    connection = main.get_db()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM batches").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM batches WHERE status = 'importing'"
        ).fetchone()[0] == 0
    finally:
        connection.close()
    assert client.get("/batch/2").status_code == 404


def test_export_csv_has_bom_and_streams_many_rows(client, monkeypatch):
    import main
    monkeypatch.setattr(main, "IMPORT_CHUNK", 1)
    lines = [CSV_TEXT.splitlines()[0]]
    for i in range(400):
        lines.append("H%03d,自购,喷药机,2,无,,,1%03d" % (i, i))
    upload(client, csv_text="\n".join(lines) + "\n")
    data = client.get("/batch/1/export.csv").data
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert len(text.splitlines()) == 401
