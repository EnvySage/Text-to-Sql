"""测试夹具：一个微型销售库。

不依赖 BIRD。目的是在真实数据集下载之前，就把 sandbox 和比对逻辑验证正确——
否则等 BIRD 到位才发现指标算错，前面跑的钱全白花。
"""

from __future__ import annotations

import sqlite3

import pytest

SCHEMA = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER,
    amount REAL,
    status TEXT
);
INSERT INTO customers VALUES (1,'Alice','East'),(2,'Bob','West'),(3,'Cara','East');
INSERT INTO orders VALUES
    (1,1,100.0,'paid'),(2,1,50.5,'paid'),(3,2,200.0,'refunded'),
    (4,3,100.0,'paid'),(5,3,NULL,'pending');
"""


@pytest.fixture(scope="session")
def sales_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "sales.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path
