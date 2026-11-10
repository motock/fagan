"""``SqliteStore`` keeps usage records in a table, not in a JSONL sidecar.

ADR 0012 decision 3 makes usage an append-only accounting artefact whose field
set is exactly :data:`pipeline.store_records.USAGE_RECORD_FIELDS`.  This story
moves the sqlite-backed record store's usage off the file store's
``<plan>.usage.jsonl`` sidecar and into a ``usage`` table with one typed column
per field, so the schema enforces the field set as well as the validator does.

The behaviour is graded through the public API (a record store's
``append_usage`` / ``get_usage``) and through the database the store writes
(the table's shape and the rows it holds).  The append-only rule is graded by
scanning the production sources for an ``UPDATE``/``DELETE`` against the
table: there must be no way to rewrite a usage row, not even internally.  The
scan looks at the SQL a module can actually run (its string literals) rather
than at prose, so a comment that merely mentions the rule is not a failure.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import pipeline.persistence

# Must precede every other ``pipeline`` import: a pre-existing import cycle
# requires ``pipeline.server`` to be imported first.
import pipeline.server
from pipeline.store_contract_usage import UsageContract, usage_record
from pipeline.store_records import (
    DEFAULT_NAMESPACE,
    USAGE_RECORD_FIELDS,
    InvalidRecordKeyError,
    InvalidUsageRecordError,
    UnknownNamespaceError,
)
from pipeline.store_sqlite import SqliteStore

PLAN = 'plan-a'
_OTHER_NAMESPACE = 'acme'
_INVALID_NAMESPACE = 'Bad NS'

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REFERENCE = _REPO_ROOT / 'REFERENCE.md'
_TELEMETRY_CONTRACT = _REPO_ROOT / 'docs' / 'TELEMETRY_CONTRACT.md'

#: The line budget of ``pipeline/store_sqlite.py``.  The usage table's code
#: belongs in ``pipeline/store_sqlite_usage.py``, so the store itself must not
#: grow to hold it.
_STORE_SQLITE_LINE_BUDGET = 721

#: The numeric fields of a usage record: a count or a measure, never text.
_NUMERIC_FIELDS = (
    'input_tokens',
    'output_tokens',
    'cache_creation_input_tokens',
    'cache_read_input_tokens',
    'duration_ms',
    'cost_usd',
)

#: The fields of a usage record that carry text (or nothing at all).
_TEXT_FIELDS = (
    'ts',
    'plan',
    'story_key',
    'correlation_id',
    'role',
    'backend',
    'model',
    'served_model',
)


# -- fixtures ---------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    """The absolute database path every store in one test shares."""
    return str(tmp_path / 's.db')


@pytest.fixture()
def plan_dir(db_path, monkeypatch):
    """Isolate ``PLAN_DIR`` so a test never writes into the real one."""
    directory = Path(db_path + '.plans')
    directory.mkdir()
    monkeypatch.setattr(pipeline.server, 'PLAN_DIR', directory)
    monkeypatch.setattr(pipeline.persistence, 'PLAN_DIR', directory)
    return directory


@pytest.fixture()
def store(db_path, plan_dir):
    """A sqlite store on a fresh database, closed after the test."""
    store = SqliteStore(db_path)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def records(store):
    """The namespaced record view of :func:`store`."""
    return store.records()


# -- helpers ----------------------------------------------------------------


def _usage_columns(db_path: str) -> dict:
    """Return the ``usage`` table's columns, keyed by name.

    Each value is the ``PRAGMA table_info`` row:
    ``(cid, name, type, notnull, default, pk)``.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute('PRAGMA table_info(usage)').fetchall()
    finally:
        conn.close()
    return {row[1]: row for row in rows}


def _usage_row_count(db_path: str) -> int:
    """Count the rows of the ``usage`` table, out of band from the store.

    A database with no ``usage`` table holds no usage rows, so it counts zero
    rather than raising: the row count is what the test grades, and a missing
    table is an empty table.
    """
    conn = sqlite3.connect(db_path)
    try:
        try:
            return conn.execute('SELECT count(*) FROM usage').fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    finally:
        conn.close()


def _declared_type(columns: dict, field: str) -> str:
    """Return the declared type of ``field``'s column."""
    assert field in columns, f'the usage table has no {field} column'
    return columns[field][2]


def _affinity(declared_type: str) -> str:
    """Return the SQLite affinity of a declared column type."""
    upper = declared_type.upper()
    if 'INT' in upper:
        return 'INTEGER'
    if 'CHAR' in upper or 'CLOB' in upper or 'TEXT' in upper:
        return 'TEXT'
    if 'BLOB' in upper or not upper:
        return 'BLOB'
    if 'REAL' in upper or 'FLOA' in upper or 'DOUB' in upper:
        return 'REAL'
    return 'NUMERIC'


# -- the shared usage contract ---------------------------------------------


class TestSqliteUsageContract(UsageContract):
    """The shared usage contract, run against the sqlite-backed record view."""

    @pytest.fixture(autouse=True)
    def _view(self, store):
        self._view = store.records()
        yield self._view

    def make_store(self):
        return self._view

    def plans_dir(self):
        return pipeline.server.PLAN_DIR


# -- the table's shape ------------------------------------------------------


class TestUsageTable:
    """The ``usage`` table: one typed column per usage record field."""

    def test_the_table_exists_and_holds_the_appended_record(self, records, db_path):
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        columns = _usage_columns(db_path)
        assert columns, (
            "the usage table must exist with typed columns after an append"
        )
        assert _usage_row_count(db_path) == 1

    def test_every_usage_field_has_its_own_column(self, records, db_path):
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        columns = _usage_columns(db_path)
        missing = [field for field in USAGE_RECORD_FIELDS if field not in columns]
        assert missing == [], f'the usage table is missing columns: {missing}'

    def test_the_table_holds_no_json_blob_column(self, records, db_path):
        """The fields are columns; nothing stores the record as one blob."""
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        columns = _usage_columns(db_path)
        allowed = {'id', 'namespace'} | set(USAGE_RECORD_FIELDS)
        assert set(columns) <= allowed, f'unexpected columns: {set(columns) - allowed}'

    def test_the_id_column_is_an_integer_primary_key(self, records, db_path):
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        columns = _usage_columns(db_path)
        assert 'id' in columns, 'the usage table has no id column'
        assert columns['id'][2].upper() == 'INTEGER'
        assert columns['id'][5] == 1, 'the id column must be the primary key'

    def test_the_id_column_never_reuses_an_id(self, records, db_path):
        """The ids are unique, so one row can never shadow another."""
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        conn = sqlite3.connect(db_path)
        try:
            ids = [row[0] for row in conn.execute('SELECT id FROM usage')]
        finally:
            conn.close()
        assert len(ids) == 2
        assert len(set(ids)) == 2

    def test_the_namespace_column_is_present_and_required(self, records, db_path):
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        columns = _usage_columns(db_path)
        assert 'namespace' in columns, 'the usage table has no namespace column'
        assert columns['namespace'][3] == 1, 'the namespace column must be NOT NULL'

    def test_the_counts_and_measures_are_typed_columns(self, records, db_path):
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        columns = _usage_columns(db_path)
        for field in _NUMERIC_FIELDS:
            affinity = _affinity(_declared_type(columns, field))
            assert affinity in {'INTEGER', 'REAL', 'NUMERIC'}, (
                f'{field} must be a typed numeric column, not {columns[field][2]!r}'
            )

    def test_the_names_and_ids_are_text_columns(self, records, db_path):
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        columns = _usage_columns(db_path)
        for field in _TEXT_FIELDS:
            affinity = _affinity(_declared_type(columns, field))
            assert affinity == 'TEXT', (
                f'{field} must be a text column, not {columns[field][2]!r}'
            )

    def test_each_field_column_holds_that_field_s_value(self, records, db_path):
        """The columns carry the fields; nothing is packed into one column."""
        record = usage_record(PLAN, story_key=None)
        records.append_usage(DEFAULT_NAMESPACE, PLAN, record)
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM usage').fetchone()
        finally:
            conn.close()
        assert row is not None
        for field in USAGE_RECORD_FIELDS:
            assert row[field] == record[field], f'{field} was not stored in its column'
        assert row['namespace'] == DEFAULT_NAMESPACE
        assert row['plan'] == PLAN
        assert isinstance(row['input_tokens'], int)
        assert isinstance(row['cost_usd'], float)


# -- the values --------------------------------------------------------------


class TestUsageValues:
    """What is written comes back as it was written."""

    def test_a_record_round_trips_with_its_types(self, records):
        record = usage_record(
            PLAN,
            story_key=None,
            attempt=None,
            correlation_id=None,
            served_model=None,
        )
        records.append_usage(DEFAULT_NAMESPACE, PLAN, record)
        (stored,) = records.get_usage(DEFAULT_NAMESPACE, PLAN)
        assert isinstance(stored['input_tokens'], int)
        assert not isinstance(stored['input_tokens'], bool)
        assert isinstance(stored['duration_ms'], int)
        assert isinstance(stored['cost_usd'], float)
        assert stored['story_key'] is None
        assert stored['attempt'] is None
        assert stored['correlation_id'] is None
        assert stored['served_model'] is None

    def test_usage_is_returned_in_id_order(self, records, db_path):
        stamps = [f'2026-11-05T10:12:{index:02d}.000Z' for index in range(3)]
        for stamp in stamps:
            records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN, ts=stamp))
        stored = records.get_usage(DEFAULT_NAMESPACE, PLAN)
        assert [record['ts'] for record in stored] == stamps
        conn = sqlite3.connect(db_path)
        try:
            ids = [row[0] for row in conn.execute('SELECT id FROM usage ORDER BY id')]
        finally:
            conn.close()
        assert len(ids) == 3
        assert ids == sorted(ids)
        assert len(set(ids)) == 3, 'every usage row needs its own id'


# -- the transaction ---------------------------------------------------------


class TestUsageTransactions:
    """A usage append joins the transaction that holds it."""

    def test_a_usage_append_is_invisible_until_its_transaction_commits(
        self, db_path, plan_dir
    ):
        writer = SqliteStore(db_path)
        try:
            writer.records().append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
            reader = SqliteStore(db_path)
            try:
                second = usage_record(PLAN, ts='2026-11-05T10:12:34.000Z')
                with writer.records().transaction(DEFAULT_NAMESPACE, PLAN) as acquired:
                    assert acquired is True
                    writer.records().append_usage(DEFAULT_NAMESPACE, PLAN, second)
                    assert reader.records().get_usage(DEFAULT_NAMESPACE, PLAN) == [
                        usage_record(PLAN)
                    ]
                assert reader.records().get_usage(DEFAULT_NAMESPACE, PLAN) == [
                    usage_record(PLAN),
                    second,
                ]
            finally:
                reader.close()
        finally:
            writer.close()

    def test_a_raising_block_leaves_no_usage_row(self, records):
        with pytest.raises(RuntimeError), records.transaction(DEFAULT_NAMESPACE, PLAN):
            records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
            raise RuntimeError('boom')
        assert records.get_usage(DEFAULT_NAMESPACE, PLAN) == []

    def test_a_crash_before_the_commit_leaves_no_usage_row(self, db_path, plan_dir):
        result = _run_child(_CRASH_CHILD, db_path, plan_dir)
        assert result.returncode == 9, result.stderr
        store = SqliteStore(db_path)
        try:
            assert store.records().get_usage(DEFAULT_NAMESPACE, PLAN) == []
        finally:
            store.close()
        assert _usage_row_count(db_path) == 0


# -- the refusals ------------------------------------------------------------


class TestUsageRefusals:
    """A refused record leaves the table exactly as it was."""

    def test_a_record_with_an_extra_key_is_refused_before_anything_is_written(
        self, records, db_path
    ):
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        with pytest.raises(InvalidUsageRecordError):
            records.append_usage(
                DEFAULT_NAMESPACE, PLAN, usage_record(PLAN, extra='not-a-field')
            )
        assert records.get_usage(DEFAULT_NAMESPACE, PLAN) == [usage_record(PLAN)]
        assert _usage_row_count(db_path) == 1

    def test_a_record_missing_a_field_is_refused(self, records, db_path):
        record = usage_record(PLAN)
        del record['cost_usd']
        with pytest.raises(InvalidUsageRecordError):
            records.append_usage(DEFAULT_NAMESPACE, PLAN, record)
        assert records.get_usage(DEFAULT_NAMESPACE, PLAN) == []
        assert _usage_row_count(db_path) == 0

    def test_an_invalid_namespace_is_refused(self, records, db_path):
        with pytest.raises(InvalidRecordKeyError):
            records.get_usage(_INVALID_NAMESPACE, PLAN)
        with pytest.raises(InvalidRecordKeyError):
            records.append_usage(_INVALID_NAMESPACE, PLAN, usage_record(PLAN))
        assert _usage_row_count(db_path) == 0

    def test_another_namespace_is_refused_or_serves_nothing(self, records):
        try:
            rows = records.get_usage(_OTHER_NAMESPACE, PLAN)
        except UnknownNamespaceError:
            return
        assert rows == []


# -- the file store's sidecar ------------------------------------------------


class TestUsageIsNotTheJsonlFile:
    """The sqlite store's usage is the table, not the file store's sidecar."""

    def test_the_sqlite_store_does_not_write_a_usage_jsonl_file(
        self, records, plan_dir
    ):
        records.append_usage(DEFAULT_NAMESPACE, PLAN, usage_record(PLAN))
        assert not (plan_dir / f'{PLAN}.usage.jsonl').exists()

    def test_an_existing_usage_jsonl_file_is_not_imported(self, db_path, plan_dir):
        (plan_dir / f'{PLAN}.usage.jsonl').write_text(
            json.dumps(usage_record(PLAN)) + '\n', encoding='utf-8'
        )
        store = SqliteStore(db_path)
        try:
            assert store.records().get_usage(DEFAULT_NAMESPACE, PLAN) == []
        finally:
            store.close()


# -- append only -------------------------------------------------------------


#: A SQL statement that rewrites a usage row.  Both are matched against the
#: start of a statement, so prose that merely mentions the rule is not one.
_UPDATE_USAGE = re.compile(r'update\s+(?:main\.)?usage\s+set\b', re.IGNORECASE)
_DELETE_USAGE = re.compile(r'delete\s+from\s+(?:main\.)?usage\b', re.IGNORECASE)
#: An upsert would rewrite a row that already exists, which usage never does.
_UPSERT_USAGE = re.compile(
    r'insert\s+(?:or\s+\w+\s+)?into\s+(?:main\.)?usage\b.*?\bon\s+conflict\b.*?\bdo\s+update\b',
    re.IGNORECASE | re.DOTALL,
)

def _sql_literals(path: Path):
    """Yield every string literal of ``path`` that is not a docstring.

    A module's SQL lives in string literals; prose about the rule lives in a
    docstring or a comment, and neither can run.
    """
    tree = ast.parse(path.read_text(encoding='utf-8', errors='replace'))
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def _rewrites_a_usage_row(literal: str) -> bool:
    """Return whether ``literal`` is a statement that rewrites a usage row.

    A statement is recognised by its leading keyword, so prose that merely
    mentions the rule is not mistaken for one.
    """
    text = literal.strip()
    # A statement may carry leading SQL comments.
    while text.startswith('--'):
        _, _, text = text.partition('\n')
        text = text.strip()
    return bool(
        _UPDATE_USAGE.match(text)
        or _DELETE_USAGE.match(text)
        or _UPSERT_USAGE.match(text)
    )


class TestUsageIsAppendOnly:
    """Nothing rewrites a usage row -- not even internally."""

    def test_no_production_module_updates_or_deletes_a_usage_row(self):
        offenders = []
        sources = list((_REPO_ROOT / 'pipeline').rglob('*.py'))
        sources += list((_REPO_ROOT / 'app').rglob('*.py'))
        sources += list((_REPO_ROOT / 'scripts').rglob('*.py'))
        sources += list(_REPO_ROOT.glob('*.py'))
        for path in sorted(sources):
            for literal in _sql_literals(path):
                if _rewrites_a_usage_row(literal):
                    offenders.append(str(path.relative_to(_REPO_ROOT)))
                    break
        assert offenders == [], f'usage rows may not be rewritten: {offenders}'

    def test_the_usage_table_code_lives_in_its_own_module(self):
        assert (_REPO_ROOT / 'pipeline' / 'store_sqlite_usage.py').is_file()

    def test_the_usage_table_is_not_defined_in_the_store_module(self):
        """The table's schema belongs in the new module, not in the store."""
        path = _REPO_ROOT / 'pipeline' / 'store_sqlite.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if 'create table' not in node.value.lower():
                continue
            assert 'usage' not in node.value.lower(), (
                'the usage table belongs in pipeline/store_sqlite_usage.py'
            )


# -- the docs ----------------------------------------------------------------


def _normalized(path: Path) -> str:
    """Return the document's text with its line wrapping collapsed."""
    return ' '.join(path.read_text(encoding='utf-8').split())


def _billing_grade_sentences(text: str) -> list[str]:
    """Return the sentences that call something the billing-grade source."""
    return [
        sentence
        for sentence in re.split(r'(?<=[.!?])\s+', text)
        if 'billing-grade source of truth' in sentence
    ]


class TestUsageDocumentation:
    """The docs must not call the JSONL the billing-grade source any more."""

    def test_reference_names_the_usage_record_as_the_billing_grade_source(self):
        text = _normalized(_REFERENCE)
        mentions = _billing_grade_sentences(text)
        assert mentions, 'REFERENCE.md must name a billing-grade source of truth'
        assert any('the usage record' in mention for mention in mentions), (
            'REFERENCE.md must call the usage record the billing-grade source'
        )
        assert 'usage jsonl remains' not in text.lower()
        assert 'that jsonl stays' not in text.lower()

    def test_the_telemetry_contract_names_the_usage_record_as_the_billing_grade_source(
        self,
    ):
        text = _normalized(_TELEMETRY_CONTRACT)
        mentions = _billing_grade_sentences(text)
        assert mentions, 'TELEMETRY_CONTRACT.md must name a billing-grade source'
        assert any('the usage record' in mention for mention in mentions), (
            'TELEMETRY_CONTRACT.md must call the usage record the billing-grade source'
        )
        assert 'usage jsonl remains' not in text.lower()
        assert 'that jsonl stays' not in text.lower()


# -- the crash child ---------------------------------------------------------

#: A child that appends a usage record inside a transaction and dies before
#: the transaction commits.  It exits with 9 so the parent can tell it got
#: that far (an uncaught exception would exit 1).
_CRASH_CHILD = """
import os
import sys

from pipeline.store_contract_usage import usage_record
from pipeline.store_sqlite import SqliteStore

records = SqliteStore(sys.argv[1]).records()
with records.transaction('default', 'plan-a'):
    records.append_usage('default', 'plan-a', usage_record('plan-a'))
    os._exit(9)
"""


def _run_child(script: str, db_path: str, plan_dir: Path) -> subprocess.CompletedProcess:
    """Run ``script`` in a child process over the database at ``db_path``."""
    env = dict(os.environ)
    env['PLAN_DIR'] = str(plan_dir)
    env['PYTHONPATH'] = os.pathsep.join(
        [str(_REPO_ROOT), env.get('PYTHONPATH', '')]
    ).strip(os.pathsep)
    return subprocess.run(
        [sys.executable, '-c', script, db_path],
        check=False,
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
