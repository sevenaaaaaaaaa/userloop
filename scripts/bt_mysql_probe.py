import sqlite3


def go() -> None:
    c = sqlite3.connect("/www/server/panel/data/default.db")
    cols = [r[1] for r in c.execute("PRAGMA table_info(config)").fetchall()]
    print("config cols:", cols)
    row = c.execute("SELECT * FROM config LIMIT 1").fetchone()
    for name, val in zip(cols, row):
        if val and ("mysql" in name.lower() or "pass" in name.lower()):
            print(name, "len=", len(str(val)), "prefix=", str(val)[:2] + "***")
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print("tables:", tables[:15])
    for t in ("databases", "database"):
        if t in tables:
            try:
                rows = c.execute(f"SELECT * FROM {t} LIMIT 3").fetchall()
                print(t, "->", [str(r)[:120] for r in rows])
            except Exception as exc:
                print(t, "err", exc)


go()
