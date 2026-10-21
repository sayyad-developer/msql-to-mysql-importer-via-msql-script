"""Make an SSMS "Generate Scripts" file runnable on SQL Server for Linux (Docker).

- Removes the leading DROP section of "Script DROP and CREATE" files.
- Strips the Windows FILENAME/ON PRIMARY/LOG ON clauses from CREATE DATABASE,
  so SQL Server uses its default Linux data folder.
- Removes Windows users (CREATE USER ...) and their role memberships,
  which cannot exist on Linux.
- Converts UTF-16 (SSMS "Unicode") to UTF-8 for sqlcmd -f 65001.

Usage: python patch_sql_script.py input.sql output.sql
"""
import re
import sys

src, dst = sys.argv[1], sys.argv[2]

with open(src, "rb") as f:
    raw = f.read()
if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
    text = raw.decode("utf-16")
else:
    text = raw.decode("utf-8-sig")

# "Script DROP and CREATE" files start with DROP statements that would run against
# master on a fresh server; keep only what follows CREATE DATABASE.
n_drop = 0
create_at = text.find("CREATE DATABASE [")
if create_at > 0 and "DROP DATABASE [" in text[:create_at]:
    n_drop = text[:create_at].count("\nDROP ") + text[:create_at].count(" DROP CONSTRAINT ")
    text = "USE [master]\r\nGO\r\n" + text[create_at:]

# CREATE DATABASE [x] CONTAINMENT ... ON PRIMARY (...) LOG ON (...)  ->  CREATE DATABASE [x]
text, n_db = re.subn(
    r"(CREATE DATABASE \[[^\]]+\])\s+CONTAINMENT\s*=\s*\w+\s+ON\s+PRIMARY\s*\(.*?\)\s*LOG ON\s*\(.*?\)\s*(?=\r?\n)",
    r"\1", text, count=1, flags=re.S)

lines = text.splitlines(keepends=True)
kept, n_users = [], 0
for line in lines:
    if re.match(r"\s*CREATE USER \[", line) or re.match(r"\s*ALTER ROLE \[\w+\] ADD MEMBER \[", line):
        n_users += 1
        continue
    kept.append(line)

with open(dst, "w", encoding="utf-8", newline="") as f:
    f.writelines(kept)

if n_drop:
    print(f"leading DROP section removed: {n_drop} statements")
print(f"CREATE DATABASE file paths removed: {'yes' if n_db else 'NOT FOUND'}")
print(f"user/role lines removed: {n_users}")
print(f"written: {dst}")
