"""Migrate a SQL Server database to MySQL 5.7.

Usage: python migrate_mssql_to_mysql.py [mysql_db_name] [--drop]
Connection settings come from env vars (defaults match the local Docker/DBngin setup):
  MSSQL_HOST, MSSQL_PORT, MSSQL_USER, MSSQL_PASSWORD, MSSQL_DB
  MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD
"""
import os
import re
import sys
import uuid
from decimal import Decimal

import pymssql
import pymysql

SRC_DB = os.environ.get("MSSQL_DB", "gsofttablet_customer65")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
DST_DB = args[0] if args else SRC_DB
DROP_EXISTING = "--drop" in sys.argv
BATCH = 1000
MAX_KEY_CHARS = 768  # 3072-byte InnoDB key limit / 4 bytes per utf8mb4 char

notes = []

ms = pymssql.connect(server=os.environ.get("MSSQL_HOST", "localhost"),
                     port=int(os.environ.get("MSSQL_PORT", "1433")),
                     user=os.environ.get("MSSQL_USER", "sa"),
                     password=os.environ.get("MSSQL_PASSWORD", "SqlDemo#2026Strong"), database=SRC_DB)
mc = ms.cursor(as_dict=True)


def q(sql, *args):
    mc.execute(sql, args or None)
    return mc.fetchall()


my = pymysql.connect(host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
                     port=int(os.environ.get("MYSQL_PORT", "3306")),
                     user=os.environ.get("MYSQL_USER", "root"),
                     password=os.environ.get("MYSQL_PASSWORD", ""), charset="utf8mb4", autocommit=False)
yc = my.cursor()


def x(sql, args=None):
    yc.execute(sql, args)


# ---------- database ----------
yc.execute("SHOW DATABASES LIKE %s", (DST_DB,))
if yc.fetchone():
    if not DROP_EXISTING:
        sys.exit(f"MySQL database {DST_DB} already exists; rerun with --drop to replace it")
    x(f"DROP DATABASE `{DST_DB}`")
x(f"CREATE DATABASE `{DST_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
x(f"USE `{DST_DB}`")
x("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_AUTO_VALUE_ON_ZERO,NO_ENGINE_SUBSTITUTION'")
x("SET SESSION foreign_key_checks=0")
x("SET SESSION time_zone='+00:00'")

# ---------- metadata ----------
tables = [r["name"] for r in q("SELECT name FROM sys.tables WHERE is_ms_shipped=0 ORDER BY name")]

cols = q("""
SELECT t.name tbl, c.name col, c.column_id, ty.name typ, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, dc.definition dflt,
       CAST(IDENT_CURRENT(SCHEMA_NAME(t.schema_id)+'.'+t.name) AS bigint) ident_cur
FROM sys.columns c
JOIN sys.tables t ON t.object_id=c.object_id
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
LEFT JOIN sys.default_constraints dc ON dc.object_id=c.default_object_id
WHERE t.is_ms_shipped=0
ORDER BY t.name, c.column_id""")

idx_rows = q("""
SELECT t.name tbl, i.name idx, i.is_primary_key pk, i.is_unique uq, i.has_filter, i.filter_definition flt,
       c.name col, ic.key_ordinal, ic.is_descending_key descn
FROM sys.indexes i
JOIN sys.tables t ON t.object_id=i.object_id
JOIN sys.index_columns ic ON ic.object_id=i.object_id AND ic.index_id=i.index_id
JOIN sys.columns c ON c.object_id=ic.object_id AND c.column_id=ic.column_id
WHERE t.is_ms_shipped=0 AND i.type>0 AND ic.is_included_column=0
ORDER BY t.name, i.name, ic.key_ordinal""")

fk_rows = q("""
SELECT fk.name fk, tp.name tbl, cp.name col, tr.name rtbl, cr.name rcol, fkc.constraint_column_id ord,
       fk.delete_referential_action_desc del, fk.update_referential_action_desc upd
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id=fk.object_id
JOIN sys.tables tp ON tp.object_id=fk.parent_object_id
JOIN sys.columns cp ON cp.object_id=fkc.parent_object_id AND cp.column_id=fkc.parent_column_id
JOIN sys.tables tr ON tr.object_id=fk.referenced_object_id
JOIN sys.columns cr ON cr.object_id=fkc.referenced_object_id AND cr.column_id=fkc.referenced_column_id
ORDER BY fk.name, fkc.constraint_column_id""")

# group
tcols = {t: [] for t in tables}
for c in cols:
    tcols[c["tbl"]].append(c)

indexes = {}
for r in idx_rows:
    key = (r["tbl"], r["idx"])
    if key not in indexes:
        indexes[key] = dict(tbl=r["tbl"], name=r["idx"], pk=r["pk"], uq=r["uq"],
                            has_filter=r["has_filter"], flt=r["flt"], cols=[])
    indexes[key]["cols"].append(r["col"])

indexed_cols = {(i["tbl"], c) for i in indexes.values() for c in i["cols"]}
fk_cols = {(r["tbl"], r["col"]) for r in fk_rows} | {(r["rtbl"], r["rcol"]) for r in fk_rows}


# ---------- type mapping ----------
def nchars(c):
    return c["max_length"] // 2


def map_type(c):
    t = c["typ"]
    if t in ("int", "bigint", "smallint"):
        return {"int": "INT", "bigint": "BIGINT", "smallint": "SMALLINT"}[t]
    if t == "tinyint":
        return "TINYINT UNSIGNED"
    if t == "bit":
        return "TINYINT(1)"
    if t == "date":
        return "DATE"
    if t in ("datetime2", "datetime", "smalldatetime"):
        return f"DATETIME({min(c['scale'], 6)})" if t == "datetime2" else "DATETIME(3)"
    if t in ("decimal", "numeric"):
        return f"DECIMAL({c['precision']},{c['scale']})"
    if t == "money":
        return "DECIMAL(19,4)"
    if t == "float":
        return "DOUBLE"
    if t == "uniqueidentifier":
        return "CHAR(36)"
    if t in ("nvarchar", "varchar", "nchar", "char"):
        if c["max_length"] == -1:
            return "LONGTEXT"
        n = nchars(c) if t.startswith("n") else c["max_length"]
        key = (c["tbl"], c["col"])
        if n <= 255 or (key in indexed_cols and n <= MAX_KEY_CHARS):
            return f"{'CHAR' if t.endswith('char') and not t.endswith('varchar') else 'VARCHAR'}({n})"
        return "TEXT" if n <= 16383 else "MEDIUMTEXT"
    if t in ("varbinary", "binary", "image"):
        return "LONGBLOB"
    raise ValueError(f"unmapped type {t} on {c['tbl']}.{c['col']}")


def map_default(c, mytype):
    d = c["dflt"]
    if d is None:
        return None
    s = d.strip()
    while s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
    m = re.fullmatch(r"CONVERT\(\[bit\],\((\d)\)\)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return s
    m = re.fullmatch(r"N?'(.*)'", s, re.S)
    if m:
        if "TEXT" in mytype:
            notes.append(f"default {d} dropped on {c['tbl']}.{c['col']} (MySQL 5.7 TEXT has no defaults)")
            return None
        return "'" + m.group(1).replace("\\", "\\\\") + "'"
    if s.lower() in ("getdate()", "sysdatetime()") and mytype.startswith("DATETIME"):
        fsp = re.search(r"\((\d)\)", mytype).group(1)
        return f"CURRENT_TIMESTAMP({fsp})" if fsp != "0" else "CURRENT_TIMESTAMP"
    notes.append(f"default {d} dropped on {c['tbl']}.{c['col']} (no MySQL 5.7 equivalent)")
    return None


# ---------- create tables ----------
coltypes = {}
for t in tables:
    defs = []
    pk = next((i for i in indexes.values() if i["tbl"] == t and i["pk"]), None)
    auto_col, auto_next = None, None
    for c in tcols[t]:
        mt = map_type(c)
        coltypes[(t, c["col"])] = (c["typ"], mt)
        line = f"`{c['col']}` {mt}"
        line += " NULL" if c["is_nullable"] else " NOT NULL"
        if c["is_identity"]:
            line += " AUTO_INCREMENT"
            auto_col = c["col"]
            auto_next = (c["ident_cur"] or 0) + 1
        else:
            dv = map_default(c, mt)
            if dv is not None:
                line += f" DEFAULT {dv}"
        defs.append(line)
    if pk:
        defs.append("PRIMARY KEY (" + ", ".join(f"`{x_}`" for x_ in pk["cols"]) + ")")
    if auto_col and (not pk or pk["cols"][0] != auto_col):
        defs.append(f"KEY `ix_auto_{auto_col}` (`{auto_col}`)")
    opts = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci ROW_FORMAT=DYNAMIC"
    if auto_col:
        opts += f" AUTO_INCREMENT={auto_next}"
    x(f"CREATE TABLE `{t}` (\n  " + ",\n  ".join(defs) + f"\n) {opts}")
print(f"created {len(tables)} tables")


# ---------- copy data ----------
def conv(v):
    if isinstance(v, uuid.UUID):
        return str(v).upper()
    if isinstance(v, bool):
        return int(v)
    return v


src_counts = {}
for t in tables:
    names = [c["col"] for c in tcols[t]]
    sel = ", ".join(f"[{n}]" for n in names)
    ins = (f"INSERT INTO `{t}` (" + ", ".join(f"`{n}`" for n in names) + ") VALUES ("
           + ", ".join(["%s"] * len(names)) + ")")
    mc2 = ms.cursor()
    mc2.execute(f"SELECT {sel} FROM [dbo].[{t}]")
    n = 0
    while True:
        rows = mc2.fetchmany(BATCH)
        if not rows:
            break
        yc.executemany(ins, [tuple(conv(v) for v in r) for r in rows])
        n += len(rows)
    my.commit()
    src_counts[t] = n
print(f"copied {sum(src_counts.values())} rows")


# ---------- secondary indexes ----------
for i in indexes.values():
    if i["pk"]:
        continue
    t = i["tbl"]
    unique = bool(i["uq"])
    if i["has_filter"]:
        only_not_null = all(re.fullmatch(r"\[\w+\] IS NOT NULL", p.strip())
                            for p in re.sub(r"^\(|\)$", "", i["flt"]).split(" AND "))
        if unique and not only_not_null:
            unique = False
            notes.append(f"filtered unique index {t}.{i['name']} WHERE {i['flt']} created as non-unique "
                         "(MySQL has no filtered indexes)")
    parts = []
    for col in i["cols"]:
        mt = coltypes[(t, col)][1]
        if "TEXT" in mt:
            parts.append(f"`{col}`(191)")
            notes.append(f"index {t}.{i['name']} uses a 191-char prefix of {col}")
        else:
            parts.append(f"`{col}`")
    name = i["name"][:64]
    x(f"ALTER TABLE `{t}` ADD {'UNIQUE ' if unique else ''}INDEX `{name}` ({', '.join(parts)})")
print("indexes created")

# ---------- foreign keys ----------
fks = {}
for r in fk_rows:
    fks.setdefault(r["fk"], dict(tbl=r["tbl"], rtbl=r["rtbl"], cols=[], rcols=[], del_=r["del"], upd=r["upd"]))
    fks[r["fk"]]["cols"].append(r["col"])
    fks[r["fk"]]["rcols"].append(r["rcol"])
act = {"NO_ACTION": "NO ACTION", "CASCADE": "CASCADE", "SET_NULL": "SET NULL", "SET_DEFAULT": "NO ACTION"}
for name, f in fks.items():
    x(f"ALTER TABLE `{f['tbl']}` ADD CONSTRAINT `{name[:64]}` FOREIGN KEY ("
      + ", ".join(f"`{c}`" for c in f["cols"]) + f") REFERENCES `{f['rtbl']}` ("
      + ", ".join(f"`{c}`" for c in f["rcols"]) + f") ON DELETE {act[f['del_']]} ON UPDATE {act[f['upd']]}")
print(f"{len(fks)} foreign keys created")

# ---------- sequences ----------
seqs = q("SELECT name, CAST(current_value AS bigint) cur, CAST(increment AS bigint) inc FROM sys.sequences")
if seqs:
    x("CREATE TABLE `_sequences` (`name` VARCHAR(128) NOT NULL PRIMARY KEY, `current_value` BIGINT NOT NULL, "
      "`increment` BIGINT NOT NULL DEFAULT 1) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    yc.executemany("INSERT INTO `_sequences` VALUES (%s,%s,%s)", [(s["name"], s["cur"], s["inc"]) for s in seqs])
    my.commit()
    notes.append("sequences " + ", ".join(s["name"] for s in seqs) + " stored in table _sequences")

checks = q("SELECT OBJECT_NAME(parent_object_id) tbl, name FROM sys.check_constraints")
for c in checks:
    notes.append(f"check constraint {c['tbl']}.{c['name']} not enforced (MySQL 5.7 ignores CHECK)")

x("SET SESSION foreign_key_checks=1")
my.commit()

# ---------- verify ----------
bad = []
for t in tables:
    yc.execute(f"SELECT COUNT(*) FROM `{t}`")
    mysql_n = yc.fetchone()[0]
    ms_n = q(f"SELECT COUNT(*) n FROM [dbo].[{t}]")[0]["n"]
    if not (mysql_n == ms_n == src_counts[t]):
        bad.append((t, ms_n, mysql_n))
    # numeric checksum on decimal/int columns
    num = [c["col"] for c in tcols[t] if c["typ"] in ("decimal", "int", "bigint")]
    if num and ms_n:
        ms_s = q("SELECT " + ", ".join(f"SUM(CAST([{c}] AS decimal(38,6))) AS [{c}]" for c in num)
                 + f" FROM [dbo].[{t}]")[0]
        yc.execute("SELECT " + ", ".join(f"SUM(`{c}`)" for c in num) + f" FROM `{t}`")
        my_s = yc.fetchone()
        for c, mv in zip(num, my_s):
            a, b = ms_s[c], mv
            if (a is None) != (b is None) or (a is not None and Decimal(a) != Decimal(b)):
                bad.append((t, c, a, b))

print("\nVERIFY:", "all row counts and numeric sums match" if not bad else bad)
print(f"tables={len(tables)} rows={sum(src_counts.values())}")
print("\nNOTES:")
for n in notes:
    print(" -", n)
