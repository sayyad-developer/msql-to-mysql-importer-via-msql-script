"""Compare SQL Server and MySQL copies of the same database, column by column.

Checks every table: row count, SUM of int/bigint/decimal columns, total length of text
columns, MIN/MAX of date columns, MIN of GUID columns.
Usage: python verify_mssql_vs_mysql.py   (same env vars as migrate_mssql_to_mysql.py)
"""
import os
from decimal import Decimal

import pymssql
import pymysql

DB = os.environ.get("MSSQL_DB", "gsofttablet_customer65")
MY_DB = os.environ.get("MYSQL_DB", DB)
ms = pymssql.connect(server=os.environ.get("MSSQL_HOST", "localhost"),
                     port=int(os.environ.get("MSSQL_PORT", "1433")),
                     user=os.environ.get("MSSQL_USER", "sa"),
                     password=os.environ.get("MSSQL_PASSWORD", "SqlDemo#2026Strong"), database=DB).cursor()
my = pymysql.connect(host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
                     port=int(os.environ.get("MYSQL_PORT", "3306")),
                     user=os.environ.get("MYSQL_USER", "root"),
                     password=os.environ.get("MYSQL_PASSWORD", ""), database=MY_DB, charset="utf8mb4").cursor()

ms.execute("SELECT name FROM sys.tables WHERE is_ms_shipped=0 ORDER BY name")
tables = [r[0] for r in ms.fetchall()]
ms.execute("""SELECT t.name, c.name, ty.name FROM sys.columns c JOIN sys.tables t ON t.object_id=c.object_id
              JOIN sys.types ty ON ty.user_type_id=c.user_type_id WHERE t.is_ms_shipped=0""")
columns = ms.fetchall()

diffs, checks, total_rows = [], 0, 0


def norm(row):
    return tuple(Decimal(v) if isinstance(v, (int, Decimal)) and not isinstance(v, bool) else v for v in row)


for t in tables:
    ms.execute(f"SELECT COUNT(*) FROM [{t}]")
    a = ms.fetchone()[0]
    my.execute(f"SELECT COUNT(*) FROM `{t}`")
    b = my.fetchone()[0]
    total_rows += a
    checks += 1
    if a != b:
        diffs.append((t, "ROWCOUNT", a, b))

for t, c, ty in columns:
    if ty in ("int", "bigint", "decimal"):
        ms.execute(f"SELECT SUM(CAST([{c}] AS decimal(38,6))) FROM [{t}]")
        my.execute(f"SELECT SUM(`{c}`) FROM `{t}`")
    elif ty == "nvarchar":
        ms.execute(f"SELECT SUM(CAST(DATALENGTH([{c}]) AS bigint))/2, COUNT([{c}]) FROM [{t}]")
        my.execute(f"SELECT SUM(CHAR_LENGTH(`{c}`)), COUNT(`{c}`) FROM `{t}`")
    elif ty in ("datetime2", "date"):
        ms.execute(f"SELECT MIN([{c}]), MAX([{c}]), COUNT([{c}]) FROM [{t}]")
        my.execute(f"SELECT MIN(`{c}`), MAX(`{c}`), COUNT(`{c}`) FROM `{t}`")
    elif ty == "uniqueidentifier":
        ms.execute(f"SELECT MIN(CAST([{c}] AS char(36))), COUNT([{c}]) FROM [{t}]")
        my.execute(f"SELECT MIN(`{c}`), COUNT(`{c}`) FROM `{t}`")
    else:
        continue
    checks += 1
    a, b = norm(ms.fetchone()), norm(my.fetchone())
    if a != b:
        diffs.append((t, c, a, b))

print(f"tables: {len(tables)}  rows: {total_rows}  checks: {checks}  differences: {len(diffs)}")
for d in diffs:
    print("DIFF", d)
print("RESULT:", "MATCH" if not diffs else "MISMATCH")
