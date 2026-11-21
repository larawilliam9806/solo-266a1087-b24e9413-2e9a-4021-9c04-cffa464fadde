"""CSV parsing and questionnaire building from uploaded files.

Large uploads are processed as a stream: the CSV is never decoded or stored
as a whole, and cleaned rows are yielded one at a time so a batch can be
written without holding the survey in memory.

Two passes are made over the (seekable) upload:

1. scan the header, collect the option values observed in the CSV and check
   that the resulting questionnaire is valid.  Nothing is written in this
   pass, so a bad file fails before the database is touched;
2. stream the data rows again, walking the jump rules and yielding one
   cleaned household at a time.

``parse`` keeps the historical in-memory return shape for tests and the
example preview script; the web importer uses :func:`plan_import` and
:meth:`ImportPlan.cleaned_rows` instead.
"""

from __future__ import annotations

import codecs
import contextlib
import csv
import io
import json
import os
import re
import shutil
import tempfile

import engine

HOUSEHOLD_HEADERS = ("户号", "户编号", "户主编号", "农户编号", "household_no",
                     "household", "household_id")
_SEPARATORS = re.compile(r"[\s:：、，,|/\\\-—_（(]+")
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")

# Bytes inspected to choose the encoding.  Enough for the BOM and for the
# CSV dialect sniffer without loading the file.
_PREFIX_BYTES = 256 * 1024
_READ_CHUNK = 64 * 1024

# Bounds that keep bookkeeping memory constant on very large files.
_MAX_OBSERVED_PER_QUESTION = 200
_MAX_WARNING_EXAMPLES = 20


class ImportError_(ValueError):
    """An uploaded file cannot be imported."""


def decode(raw):
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_("无法识别 CSV 编码，请另存为 UTF-8 或 GBK")


def _prefix_decodes(prefix, encoding):
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        decoder.decode(prefix, final=False)
    except UnicodeDecodeError:
        return False
    return True


def detect_encoding(prefix):
    for encoding in _ENCODINGS:
        if _prefix_decodes(prefix, encoding):
            return encoding
    raise ImportError_("无法识别 CSV 编码，请另存为 UTF-8 或 GBK")


def _detection_prefix(binary):
    """Bytes used to choose the encoding, reading ahead while still ASCII.

    Pure-ASCII bytes decode under every supported encoding, so a long ASCII
    prefix (e.g. a GBK file with English headers) must not decide the
    encoding on its own: read on until the first non-ASCII chunk without
    retaining the intermediate bytes.
    """
    binary.seek(0)
    head = binary.read(_PREFIX_BYTES)
    if not head or not head.isascii():
        return head
    while True:
        chunk = binary.read(_READ_CHUNK)
        if not chunk:
            return head  # the whole file is ASCII: any encoding is fine
        if not chunk.isascii():
            return head + chunk


def _iter_text_lines(binary, encoding):
    """Yield decoded lines without reading the whole file into memory."""
    decoder = codecs.getincrementaldecoder(encoding)()
    pending = ""
    while True:
        chunk = binary.read(_READ_CHUNK)
        if not chunk:
            break
        pending += decoder.decode(chunk)
        start = 0
        while True:
            newline = pending.find("\n", start)
            if newline < 0:
                break
            yield pending[start:newline + 1]
            start = newline + 1
        if start:
            pending = pending[start:]
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


class CsvSource:
    """A re-openable CSV source that never buffers the whole file.

    ``source`` may be bytes, a path, or a binary file object.  Non-seekable
    streams are copied to a temporary file on disk (not to memory).
    """

    def __init__(self, source):
        self._bytes = None
        self._path = None
        self._stream = None
        self._temporary = None
        if isinstance(source, (bytes, bytearray, memoryview)):
            self._bytes = bytes(source)
        elif isinstance(source, (str, os.PathLike)):
            self._path = os.fspath(source)
        else:
            try:
                seekable = bool(source.seekable())
            except (AttributeError, OSError, ValueError):
                seekable = False
            if seekable:
                self._stream = source
            else:
                handle, path = tempfile.mkstemp(prefix="survey-import-",
                                                suffix=".csv")
                with os.fdopen(handle, "wb") as out:
                    shutil.copyfileobj(source, out, _READ_CHUNK)
                self._path = self._temporary = path

    def open(self):
        """Return ``(binary_stream, owned)`` positioned at the start."""
        if self._bytes is not None:
            return io.BytesIO(self._bytes), True
        if self._path is not None:
            return open(self._path, "rb"), True
        self._stream.seek(0)
        return self._stream, False

    def close(self):
        path, self._temporary = self._temporary, None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    def __del__(self):
        self.close()


def _open_reader(binary):
    """Detect the encoding/dialect and return ``(reader, encoding)``."""
    prefix = _detection_prefix(binary)
    binary.seek(0)
    encoding = detect_encoding(prefix)
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        sample = decoder.decode(prefix, final=False)[:4096]
    except UnicodeDecodeError:
        raise ImportError_("无法识别 CSV 编码，请另存为 UTF-8 或 GBK")
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return csv.reader(_iter_text_lines(binary, encoding), dialect), encoding


@contextlib.contextmanager
def _open_rows(source):
    binary, owned = source.open()
    try:
        reader, _encoding = _open_reader(binary)
        yield reader
    except UnicodeDecodeError:
        raise ImportError_("无法识别 CSV 编码，请另存为 UTF-8 或 GBK")
    except csv.Error as error:
        raise ImportError_("CSV 解析失败: %s" % error)
    finally:
        if owned:
            binary.close()


def _clean_header(header):
    return (header or "").replace("﻿", "").strip()


def find_household_column(headers):
    cleaned = [_clean_header(h) for h in headers]
    for position, header in enumerate(cleaned):
        lowered = header.lower()
        for candidate in HOUSEHOLD_HEADERS:
            if lowered == candidate.lower():
                return position
    for position, header in enumerate(cleaned):
        if "户号" in header or "编号" in header:
            return position
    return 0


def header_key(header):
    """``A1 设备来源`` -> ``("A1", "设备来源")``; ``设备来源`` -> ``(None, 设备来源)``."""
    cleaned = _clean_header(header)
    parts = _SEPARATORS.split(cleaned, maxsplit=1)
    first = parts[0].strip()
    if parts[1:]:
        rest = cleaned[len(first):].strip()
        rest = rest.lstrip("".join(" \t:：、，,|/\\-—_（）()"))
        return (first or None), rest.strip()
    return first or None, cleaned


def question_id_for_header(header, definition):
    """Map a CSV header to a question id.

    Accepts the bare id, ``A1 设备来源``, ``A1：设备来源`` and the title.
    """
    cleaned = _clean_header(header)
    ids = [question["id"] for question in definition]
    if cleaned in ids:
        return cleaned
    first = _SEPARATORS.split(cleaned, maxsplit=1)[0].strip()
    if first in ids:
        return first
    for question in definition:
        if cleaned == question["title"]:
            return question["id"]
    return None


def read_json(raw):
    try:
        return json.loads(decode(raw))
    except (ValueError, TypeError) as error:
        raise ImportError_("JSON 跳题表解析失败: %s" % error)


class _Layout:
    """Header-derived information shared by both passes."""

    def __init__(self, headers, household_position, columns, column_map):
        self.headers = headers
        self.household_position = household_position
        self.columns = columns
        self.column_map = column_map


def _read_layout(reader, json_data, household_column):
    try:
        raw_headers = next(reader)
    except StopIteration:
        raise ImportError_("CSV 是空的")
    headers = [_clean_header(h) for h in raw_headers]
    if not any(headers):
        raise ImportError_("CSV 表头为空")

    household_position = (find_household_column(headers)
                          if household_column in (None, "")
                          else int(household_column))
    if household_position < 0 or household_position >= len(headers):
        raise ImportError_("户号列位置超出范围")
    if len(headers) < 2:
        raise ImportError_("CSV 至少需要户号列和一个题目列")

    # Question order and titles come from the JSON when it declares them.
    # A bare jump table leaves the order to the CSV columns.
    json_columns = engine.json_columns(json_data) if json_data is not None \
        else None
    if json_columns:
        columns = [{"id": column["id"],
                    "title": column["title"] or column["id"]}
                   for column in json_columns]
    else:
        columns = []
        for position, header in enumerate(headers):
            if position == household_position:
                continue
            key, title = header_key(header)
            columns.append({"id": key or title, "title": title})

    column_map = {}
    for position, header in enumerate(headers):
        if position == household_position:
            continue
        qid = question_id_for_header(header, columns)
        if qid is None:
            continue
        column_map[position] = qid

    if not column_map:
        raise ImportError_("CSV 中没有能对应到题号的列")

    return _Layout(headers, household_position, columns, column_map)


def _column_definitions(headers, column_map):
    """Question columns in first-appearance order, titled by the raw header."""
    definitions = []
    seen = set()
    for position, header in enumerate(headers):
        qid = column_map.get(position)
        if qid is None or qid in seen:
            continue
        seen.add(qid)
        definitions.append({"id": qid, "title": header})
    return definitions


def _candidate_rows(reader, column_map, household_position, warnings, seen):
    """Yield ``(row_number, household_no, answers)`` for usable rows.

    Blank lines and rows without a household number are skipped; the first
    occurrence of a household number wins, matching "keep the first row in
    the whole file".  ``warnings`` may be ``None`` for a silent pass.
    """
    missing = 0
    duplicates = 0
    try:
        for row_number, raw_row in enumerate(reader, start=1):
            if not any((cell or "").strip() for cell in raw_row):
                continue
            household_no = (raw_row[household_position]
                            if household_position < len(raw_row) else "").strip()
            if not household_no:
                missing += 1
                if warnings is not None and missing <= _MAX_WARNING_EXAMPLES:
                    warnings.append(
                        "CSV 第 %d 行缺少户号，已跳过" % (row_number + 1))
                continue
            if household_no in seen:
                duplicates += 1
                if warnings is not None and \
                        duplicates <= _MAX_WARNING_EXAMPLES:
                    warnings.append(
                        "户号 %s 重复出现，只保留第一次" % household_no)
                continue
            seen.add(household_no)
            answers = {}
            for position, qid in column_map.items():
                cell = (raw_row[position]
                        if position < len(raw_row) else "").strip()
                if cell:
                    answers[qid] = cell
            yield row_number, household_no, answers
    finally:
        if warnings is not None:
            if missing > _MAX_WARNING_EXAMPLES:
                warnings.append("另有 %d 行缺少户号，已跳过"
                                % (missing - _MAX_WARNING_EXAMPLES))
            if duplicates > _MAX_WARNING_EXAMPLES:
                warnings.append(
                    "另有 %d 个重复户号，只保留第一次出现的记录"
                    % (duplicates - _MAX_WARNING_EXAMPLES))


class ImportPlan:
    """A validated import that can be streamed to storage.

    ``definition`` and ``warnings`` are available immediately.  Iterating
    :meth:`rows` / :meth:`cleaned_rows` re-reads the source one row at a
    time; nothing from the data rows is retained.
    """

    def __init__(self, source, definition, warnings, layout):
        self._source = source
        self.definition = definition
        self.warnings = warnings
        self.headers = layout.headers
        self.household_position = layout.household_position
        self._layout = layout

    def rows(self):
        """Yield ``{household_no, row_number, answers}`` lazily."""
        with _open_rows(self._source) as reader:
            next(reader, None)  # header, already validated by the scan pass
            seen = set()
            for item in _candidate_rows(
                    reader, self._layout.column_map,
                    self._layout.household_position, None, seen):
                row_number, household_no, answers = item
                yield {
                    "household_no": household_no,
                    "row_number": row_number,
                    "answers": answers,
                }

    def cleaned_rows(self):
        """Yield ``(household_no, row_number, {(prefix, qid): value})``."""
        for row in self.rows():
            _, stored = engine.import_row(self.definition, row["answers"])
            yield row["household_no"], row["row_number"], stored

    def close(self):
        self._source.close()


def plan_import(source, raw_json, household_column=None):
    """Validate an upload and return a streamable :class:`ImportPlan`.

    The scan pass reads every row but retains only bounded bookkeeping
    (seen household numbers and a capped list of observed option values).
    Raises :class:`ImportError_` before anything is written if the upload is
    unusable.
    """
    csv_source = source if isinstance(source, CsvSource) else CsvSource(source)
    warnings = []
    try:
        data = read_json(raw_json) if raw_json else None
        with _open_rows(csv_source) as reader:
            layout = _read_layout(reader, data, household_column)
            column_definitions = _column_definitions(
                layout.headers, layout.column_map)
            observed = {}
            accepted = 0
            seen = set()
            for _row_number, _household_no, answers in _candidate_rows(
                    reader, layout.column_map, layout.household_position,
                    warnings, seen):
                accepted += 1
                for qid, value in answers.items():
                    values = observed.setdefault(qid, [])
                    if len(values) < _MAX_OBSERVED_PER_QUESTION and \
                            value not in values:
                        values.append(value)
            if accepted == 0:
                raise ImportError_("CSV 中没有数据行")

        if data is None:
            definition = engine.build_definition_from_columns(
                column_definitions)
            warnings.append("未上传 JSON 跳题表：按 CSV 列顺序生成纯文本问卷")
        else:
            try:
                definition, build_warnings = engine.build_definition(
                    data, column_definitions, observed)
            except engine.DefinitionError as error:
                raise ImportError_(str(error))
            warnings.extend(build_warnings)

        return ImportPlan(csv_source, definition, warnings, layout)
    except BaseException:
        csv_source.close()
        raise


def parse(raw_csv, raw_json, household_column=None):
    """Legacy API returning ``(definition, warnings, rows, position, headers)``.

    ``rows`` is a materialised list for callers that need it twice (tests and
    the example preview).  The web application uses :func:`plan_import` and
    streams the rows instead, so it never builds this list.
    """
    plan = plan_import(raw_csv, raw_json, household_column)
    try:
        rows = list(plan.rows())
    finally:
        plan.close()
    return (plan.definition, plan.warnings, rows,
            plan.household_position, plan.headers)


def to_import_answers(definition, rows):
    """Key answers by ``(prefix, qid)`` using the routing rules."""
    stored = {}
    for row in rows:
        _, answers = engine.import_row(definition, row["answers"])
        stored[row["household_no"]] = answers
    return stored
