# SQL Server backup → SQL Server 2022 (Docker) → MySQL 5.7 — Migration Guide

How the `gsofttablet_customer65` database was moved from a client SQL Server backup into
SQL Server 2022 in Docker and then into local MySQL 5.7, with every command needed to
repeat and confirm it from the terminal.

**Final result (verified 2026-10-14):**

| | SQL Server 2022 (Docker) | MySQL 5.7 (local) |
|---|---|---|
| Database | `gsofttablet_customer65` | `gsofttablet_customer65` |
| Tables | 105 | 105 |
| Rows | 38,744 | 38,744 |
| Foreign keys | 91 | 91 |
| Column checks (counts, sums, text lengths, dates, GUIDs) | — | 1,369 checks, 0 differences |

---

## 0. What's in this folder

```
SQLSDemo/
├── backup_20261001_154942.bak      original .bak (SQL Server 2025 – cannot restore on 2022)
├── 22.sql                          SSMS-generated script (schema + data, Unicode)
├── MIGRATION_GUIDE.md              this file
└── scripts/
    ├── patch_sql_script.py         makes 22.sql runnable on SQL Server for Linux
    ├── migrate_mssql_to_mysql.py   SQL Server → MySQL migration (schema, data, indexes, FKs)
    ├── verify_mssql_vs_mysql.py    column-by-column comparison of both databases
    └── requirements.txt            Python drivers (pymssql, PyMySQL)
```

## 1. Environment

| Item | Value |
|---|---|
| Machine | Mac, Intel (x86_64) |
| Docker | 29.8.2 (amd64) |
| SQL Server | `mcr.microsoft.com/mssql/server:2022-latest`, container `sql2022` |
| SQL Server login | `localhost,1433` · user `sa` · password `SqlDemo#2026Strong` |
| MySQL | 5.7.23 (DBngin) · `127.0.0.1:3306` · user `root` · no password |
| Python | 3.9 (system) + venv with `pymssql`, `PyMySQL` |

Shortcut used throughout this guide (paste into your terminal first):

```bash
cd ~/Desktop/SQLSDemo
alias mssql="docker exec -i sql2022 /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P 'SqlDemo#2026Strong'"
```

---

## 2. Start SQL Server 2022 in Docker

```bash
docker pull mcr.microsoft.com/mssql/server:2022-latest

docker run -d --name sql2022 \
  -e ACCEPT_EULA=Y \
  -e 'MSSQL_SA_PASSWORD=SqlDemo#2026Strong' \
  -e MSSQL_PID=Developer \
  -p 1433:1433 \
  -v sql2022_data:/var/opt/mssql \
  -v ~/Desktop/SQLSDemo:/var/opt/mssql/backup:ro \
  mcr.microsoft.com/mssql/server:2022-latest
```

- `sql2022_data` is a named volume, so databases survive `docker stop` / `docker start`.
- The project folder is mounted **read-only** at `/var/opt/mssql/backup` so SQL Server can see the `.bak`.

**Confirm it is ready** (wait until this prints the line, ~10–30 s):

```bash
docker logs sql2022 2>&1 | grep "ready for client connections"
mssql -Q "SELECT @@VERSION"
```

---

## 3. Why the `.bak` could not be restored

Inspect a backup before restoring it:

```bash
mssql -W -s '|' -Q "RESTORE HEADERONLY  FROM DISK='/var/opt/mssql/backup/backup_20261001_154942.bak'"
mssql -W -s '|' -Q "RESTORE FILELISTONLY FROM DISK='/var/opt/mssql/backup/backup_20261001_154942.bak'"
```

The header showed `SoftwareVersionMajor = 17` and `DatabaseVersion = 998` → the backup was taken on
**SQL Server 2025**. SQL Server 2022 supports database version 957/958, and the restore fails:

```
Msg 3169: The database was backed up on a server running database version 998.
That version is incompatible with this server, which supports version 958.
```

SQL Server never restores a backup from a newer version. Two ways out:
- run the `mcr.microsoft.com/mssql/server:2025-latest` image instead and restore the `.bak`, **or**
- generate a **script** from the source server targeted at SQL Server 2022 (what we did).

<details>
<summary>Restore command (works when versions are compatible)</summary>

```bash
mssql -Q "RESTORE DATABASE [gsofttablet_customer65]
  FROM DISK='/var/opt/mssql/backup/backup_20261001_154942.bak'
  WITH MOVE 'gsoft'     TO '/var/opt/mssql/data/gsofttablet_customer65.mdf',
       MOVE 'gsoft_log' TO '/var/opt/mssql/data/gsofttablet_customer65_log.ldf',
       RECOVERY, STATS=10"
```
`gsoft` / `gsoft_log` are the logical names from `RESTORE FILELISTONLY`.
</details>

---

## 4. Generate the script on the source server (SSMS)

1. Right-click the database → **Tasks → Generate Scripts…**
2. **Script entire database and all database objects**
3. **Advanced**:
   - *Script for Server Version* → **SQL Server 2022**
   - *Types of data to script* → **Schema and data**
   - *Script Indexes / Triggers / Foreign Keys* → **True**
4. Save to file → **Single file**, encoding **Unicode** (keeps non-English text intact).

Result: `22.sql` — 295 MB, UTF-16, 105 tables, 2 sequences, 38,744 `INSERT` rows.

**Confirm the script contents:**

```bash
file 22.sql                                              # → Unicode text, UTF-16, little-endian
iconv -f UTF-16LE -t UTF-8 22.sql | grep -c '^INSERT'          # → 38744
iconv -f UTF-16LE -t UTF-8 22.sql | grep -c '^CREATE TABLE'    # → 105
```

---

## 5. Patch the script for SQL Server on Linux

Two parts of an SSMS script fail on Linux:

| Problem | Fix |
|---|---|
| `CREATE DATABASE … FILENAME = N'C:\Program Files\…\gsofttablet_customer65.mdf'` (Windows paths) | Remove the file clauses → SQL Server uses `/var/opt/mssql/data/` |
| Windows users: `NT AUTHORITY\SYSTEM`, `IIS APPPOOL\gsoft`, `DESKTOP-…\HP`, `BUILTIN\Administrators` | Remove `CREATE USER` + `ALTER ROLE … ADD MEMBER` lines (log in as `sa`) |

Set up Python once (a venv keeps the drivers out of the system Python):

```bash
python3 -m venv ~/.venvs/sqlmigrate
~/.venvs/sqlmigrate/bin/pip install -r scripts/requirements.txt
```

Patch (original `22.sql` is not modified; output is UTF-8):

```bash
~/.venvs/sqlmigrate/bin/python scripts/patch_sql_script.py 22.sql 22.patched.sql
```

Expected output:
```
CREATE DATABASE file paths removed: yes
user/role lines removed: 13
```

---

## 6. Load the script into SQL Server 2022

```bash
docker cp 22.patched.sql sql2022:/tmp/22.patched.sql

time docker exec sql2022 /opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa \
  -P 'SqlDemo#2026Strong' -f 65001 -i /tmp/22.patched.sql > load.log 2>&1

grep -c '^Msg' load.log        # → 0   (number of errors)
```

- `-f 65001` = read the file as UTF-8.
- Took **~1 min 50 s**. Do not open a 300 MB script in SSMS/Azure Data Studio — run it with `sqlcmd`.

**Confirm in SQL Server:**

```bash
mssql -d gsofttablet_customer65 -W -s '|' -Q "
SET NOCOUNT ON;
SELECT name, state_desc, compatibility_level FROM sys.databases WHERE name = DB_NAME();   -- ONLINE, 160
SELECT COUNT(*) AS tables_ FROM sys.tables;                                               -- 105
SELECT SUM(p.rows) AS total_rows FROM sys.tables t
  JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1);            -- 38744
SELECT COUNT(*) AS fks, SUM(CAST(is_not_trusted AS int)) AS untrusted FROM sys.foreign_keys;  -- 91, 0
"
```

`untrusted = 0` means SQL Server re-validated every foreign key against the loaded data.

---

## 7. Migrate SQL Server → MySQL 5.7

**Check MySQL first:**

```bash
mysql -uroot -h127.0.0.1 -e "SELECT VERSION(); SHOW DATABASES LIKE 'gsofttablet%';"
```

**Run the migration** (creates database `gsofttablet_customer65` in MySQL):

```bash
~/.venvs/sqlmigrate/bin/python scripts/migrate_mssql_to_mysql.py
```

Options:
```bash
# different MySQL database name
~/.venvs/sqlmigrate/bin/python scripts/migrate_mssql_to_mysql.py my_new_db
# re-run: drop and rebuild the MySQL database (refuses to overwrite without --drop)
~/.venvs/sqlmigrate/bin/python scripts/migrate_mssql_to_mysql.py --drop
# other servers / credentials
MSSQL_HOST=... MSSQL_PASSWORD=... MYSQL_HOST=... MYSQL_USER=... MYSQL_PASSWORD=... \
  ~/.venvs/sqlmigrate/bin/python scripts/migrate_mssql_to_mysql.py
```

Expected output (~35 s):
```
created 105 tables
copied 38744 rows
indexes created
91 foreign keys created

VERIFY: all row counts and numeric sums match
```

### What the script does

1. Creates the database as **`utf8mb4` / `utf8mb4_unicode_ci`** (your MySQL server default is `latin1`, which would lose non-English text).
2. Reads table/column/index/FK definitions from SQL Server system views and creates MySQL tables.
3. Copies data in batches of 1,000 rows with `STRICT_ALL_TABLES` on (so any truncation is an error, not silent) and foreign key checks off during load.
4. Adds secondary indexes, then the 91 foreign keys (validated by MySQL when re-enabled).
5. Compares row counts and numeric column sums against SQL Server.

### Type mapping

| SQL Server | MySQL 5.7 |
|---|---|
| `int` / `bigint` | `INT` / `BIGINT` |
| `bit` | `TINYINT(1)` |
| `decimal(p,s)` | `DECIMAL(p,s)` |
| `datetime2(n)` | `DATETIME(6)` (max precision in MySQL; SQL Server's 7th digit is dropped) |
| `date` | `DATE` |
| `uniqueidentifier` | `CHAR(36)` (uppercase, same as SQL Server) |
| `nvarchar(n)`, n ≤ 255 or indexed | `VARCHAR(n)` |
| `nvarchar(n)`, larger | `TEXT` |
| `nvarchar(max)` | `LONGTEXT` |
| `IDENTITY` | `AUTO_INCREMENT` (continues from SQL Server's current value) |
| `getdate()` / `sysdatetime()` default | `CURRENT_TIMESTAMP(6)` |

### Differences you need to know about (MySQL 5.7 limitations)

| SQL Server feature | In MySQL | Impact |
|---|---|---|
| 6 `CHECK` constraints (Expenses, TempAccountEntries, DayClosingCashDenominations) | Not enforced (5.7 parses and ignores CHECK) | Invalid values are no longer rejected |
| Filtered unique `UX_BusinessDays_Open_Outlet_Register` and `UX_Users_LoginPin` | Created as **non-unique** | Duplicates are no longer blocked |
| Other filtered unique indexes (`WHERE col IS NOT NULL`) | Plain unique index | Same behaviour (MySQL allows many NULLs) |
| 22 defaults: `sysutcdatetime()`, `newid()`, `CONVERT(date,getdate())`, and text defaults on `TEXT` columns | Dropped | New rows must supply these values |
| Sequences `PosInvoiceSequence` (3108), `PurchaseReferenceSequence` (5020) | Rows in table `_sequences` | No `NEXT VALUE FOR` in MySQL |
| Collation `SQL_Latin1_General_CP1_CI_AS` | `utf8mb4_unicode_ci` | Both case-insensitive; MySQL is also accent-insensitive |
| Windows users / logins | Not migrated | Use MySQL `root` |

Existing data is unaffected by all of the above; they only matter if an application writes to the MySQL copy.

---

## 8. Confirm the MySQL copy

**Full comparison against SQL Server** (row counts, sums of every number column, total length of every
text column, min/max of every date column, GUIDs):

```bash
~/.venvs/sqlmigrate/bin/python scripts/verify_mssql_vs_mysql.py
```

Expected:
```
tables: 105  rows: 38744  checks: 1369  differences: 0
RESULT: MATCH
```

**Manual checks in MySQL:**

```bash
mysql -uroot -h127.0.0.1 gsofttablet_customer65 -e "
SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME
  FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE();                     -- utf8mb4, utf8mb4_unicode_ci
SELECT COUNT(*) AS tables_ FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE();   -- 106 (105 + _sequences)
SELECT COUNT(*) AS fks FROM information_schema.REFERENTIAL_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE();                                                 -- 91
SELECT * FROM _sequences;
"
```

Exact row counts per table (note: `information_schema.TABLES.TABLE_ROWS` is only an
estimate in InnoDB — use `COUNT(*)`):

```bash
mysql -uroot -h127.0.0.1 -N gsofttablet_customer65 -e "
SELECT CONCAT('SELECT ''', TABLE_NAME, ''' t, COUNT(*) n FROM \`', TABLE_NAME, '\`')
FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()" \
| paste -sd '|' - | sed 's/|/ UNION ALL /g' \
| mysql -uroot -h127.0.0.1 gsofttablet_customer65 | sort -k2 -n -r | head -15
```

Top tables should show: BusinessSessionAuditLogs 8145, SaleItems 7445, Payments / Sales / SyncQueues 3492.

---

## 9. Day-to-day commands

```bash
docker stop sql2022            # stop SQL Server (data kept in volume)
docker start sql2022           # start it again
docker logs -f sql2022         # watch the log
docker ps -a                   # status

# full cleanup (DELETES the SQL Server data)
docker rm -f sql2022 && docker volume rm sql2022_data

# drop the MySQL copy
mysql -uroot -h127.0.0.1 -e "DROP DATABASE gsofttablet_customer65"
```

GUI connections:
- **SQL Server:** Azure Data Studio / DBeaver / VS Code mssql → `localhost,1433`, `sa`, `SqlDemo#2026Strong`, *Trust server certificate* = on.
- **MySQL:** TablePlus / DBeaver / MySQL Workbench → `127.0.0.1:3306`, `root`, no password.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Msg 3169 … database version 998 … supports version 958` | Backup is from a newer SQL Server. Use a matching image (`2025-latest`) or generate a script (§4). |
| `Login failed for user 'sa'` right after `docker run` | SQL Server still starting — wait for "ready for client connections". |
| `Directory lookup for the file "C:\…" failed` | Script not patched — run §5. |
| `Windows NT user or group … not found` | Same — `patch_sql_script.py` removes those lines. |
| `?` instead of letters in MySQL | Database created as latin1 — the script always creates utf8mb4; don't pre-create it yourself. |
| `MySQL database … already exists; rerun with --drop` | Safety stop — add `--drop` only if you want to replace it. |
| `Data too long for column` during migration | Strict mode caught a truncation — widen the mapping in `map_type()` in the migration script. |
