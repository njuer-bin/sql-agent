"""W5 UI 数据导入：上传 CSV / SQL → SQLite（_handle_uploads 纯逻辑层）。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.streamlit_app import _handle_uploads  # noqa: E402


class _File(SimpleNamespace):
    @property
    def size(self):  # 占位：sig 指纹在 UI 层用 f.size
        return len(self.getvalue())


def _mk(name: str, raw: bytes) -> _File:
    return _File(name=name, getvalue=lambda: raw)


def _tables(db: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    finally:
        con.close()


def test_csv_bom_and_gbk_fallback(tmp_path):
    db = str(tmp_path / "u1.db")
    msgs1: list = []
    msgs2: list = []
    _handle_uploads([_mk("sales.csv", "日期,金额\n2026-01-01,100\n".encode("utf-8-sig"))],
                    db, msgs1)
    _handle_uploads([_mk("销售表.csv", "城市,GMV\n北京,2.44\n".encode("gbk"))],
                    db, msgs2)
    assert _tables(db) == ["sales", "销售表"]
    assert all(k == "ok" for k, _ in msgs1 + msgs2)
    con = sqlite3.connect(db)
    try:
        rows = con.execute('SELECT 日期, 金额 FROM sales').fetchall()
        assert rows == [("2026-01-01", 100)]
    finally:
        con.close()


def test_sql_dump_executescript(tmp_path):
    db = str(tmp_path / "u2.db")
    dump = (b"BEGIN TRANSACTION;\n"
            b"CREATE TABLE orders (id INTEGER, order_date TEXT);\n"
            b"INSERT INTO orders VALUES (1, '2026-09-01');\n"
            b"CREATE TABLE refund_paid_view AS SELECT 1 AS x;\n"
            b"COMMIT;\n")
    msgs: list = []
    _handle_uploads([_mk("dump.sql", dump)], db, msgs)
    assert msgs and msgs[0][0] == "ok"
    con = sqlite3.connect(db)
    try:
        (n,) = con.execute("SELECT COUNT(*) FROM orders").fetchone()
        assert n == 1
    finally:
        con.close()


def test_bad_sql_isolated_and_others_survive(tmp_path):
    db = str(tmp_path / "u3.db")
    msgs: list = []
    files = [
        _mk("good.csv", b"a,b\n1,2\n"),
        _mk("broken.sql", b"TOTAL GARBAGE NOT SQL;\n"),
        _mk("z.sql", b"CREATE TABLE t2 (x);\n"),
    ]
    _handle_uploads(files, db, msgs)
    kinds = [k for k, _ in msgs]
    assert kinds == ["ok", "err", "ok"]
    assert _tables(db) == ["good", "t2"]


def test_same_name_csv_is_replaced(tmp_path):
    db = str(tmp_path / "u4.db")
    m1: list = []
    m2: list = []
    _handle_uploads([_mk("s.csv", b"a\n1\n")], db, m1)
    _handle_uploads([_mk("s.csv", b"a\n9\n2\n")], db, m2)
    con = sqlite3.connect(db)
    try:
        (n,) = con.execute("SELECT COUNT(*) FROM s").fetchone()
        assert n == 2  # replace 而非重复追加
    finally:
        con.close()
