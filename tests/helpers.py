"""Shared fakes and fixture loading for the test suite."""
import json
import os
from unittest import mock

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def load_fixture(name):
    """A real-shaped Zoom webhook payload from tests/fixtures/<name>.json."""
    with open(os.path.join(FIXTURES_DIR, name + '.json'), encoding='utf-8') as f:
        return json.load(f)


class FakeBQ:
    """BigQuery stand-in: insert_rows_json fails `fail_times` times then
    succeeds; captures rows and the row_ids passed for streaming dedup."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.rows = []
        self.row_ids = []
        self.calls = 0

    def insert_rows_json(self, table_id, rows, **kw):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            return [{'index': 0, 'errors': [{'reason': 'backendError'}]}]
        self.rows.extend(rows)
        self.row_ids.extend(kw.get('row_ids') or [None] * len(rows))
        return []

    def query(self, *a, **kw):
        return mock.MagicMock(result=lambda *aa, **kk: [])
