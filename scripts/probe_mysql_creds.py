import json
import re
import sys


def probe(path: str) -> None:
    try:
        s = open(path, encoding="utf-8", errors="ignore").read()
    except Exception as exc:
        print("read err", exc)
        return
    hits = {}
    for kw in ("host", "port", "user", "password", "passwd", "pass", "database", "dbname"):
        vals = re.findall(r'"[^"]*' + kw + r'[^"]*"\s*:\s*"?([^",\n}]{0,40})', s, re.I)
        if vals:
            hits[kw] = vals[:3]
    if hits:
        print(path, "->", json.dumps(hits, ensure_ascii=False)[:400])


for p in sys.argv[1:]:
    probe(p)
