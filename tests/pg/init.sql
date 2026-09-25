-- 和 tests/conftest.py 的 SQLite 夹具同一份数据，两边的测试断言可以对照着写。
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, region TEXT);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER,
    amount NUMERIC(10, 2),
    status TEXT
);
INSERT INTO customers VALUES (1, 'Alice', 'East'), (2, 'Bob', 'West'), (3, 'Cara', 'East');
INSERT INTO orders VALUES
    (1, 1, 100.0, 'paid'), (2, 1, 50.5, 'paid'), (3, 2, 200.0, 'refunded'),
    (4, 3, 100.0, 'paid'), (5, 3, NULL, 'pending');

-- BIRD 风格的大小写混合、带空格的标识符。PG 会把不加引号的名字折叠成小写，
-- 所以 schema 渲染必须把引号带出来，否则模型写出的 SQL 找不到列。
CREATE TABLE "Products" ("ProductID" INTEGER PRIMARY KEY, "Product Name" TEXT, price NUMERIC(10, 2));
INSERT INTO "Products" VALUES (1, 'Widget', 9.99), (2, 'Gadget', 19.5);

CREATE SEQUENCE order_seq;

-- 只读账号：沙箱连接层防线的前提。只给 CONNECT / USAGE / SELECT。
CREATE ROLE agent_ro LOGIN PASSWORD 'agent_ro';
GRANT CONNECT ON DATABASE sales TO agent_ro;
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;

-- 第二个 schema，同名表不同数据：验证 search_path 能把查询和 schema 渲染限定在一个库里。
-- 评测时 11 个 BIRD 库就是这样放在同一个 PG 库的不同 schema 里的。
CREATE SCHEMA shop2;
CREATE TABLE shop2.orders (id INTEGER PRIMARY KEY, amount NUMERIC(10, 2));
INSERT INTO shop2.orders VALUES (1, 1.0), (2, 2.0);
GRANT USAGE ON SCHEMA shop2 TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA shop2 TO agent_ro;
