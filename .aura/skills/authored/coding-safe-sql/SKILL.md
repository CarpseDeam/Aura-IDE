---
task_kinds: ["coding"]
path_globs: []
model: null
triggers: ["sqlite3", "psycopg", "pymysql", "cursor.execute", ".execute(", "sql", "query"]
---

Do not build SQL by formatting variables into the query string. Use parameterized queries with placeholders. A formatted query is an injection bug.
