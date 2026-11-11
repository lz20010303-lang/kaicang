"""比对 Python 与 Swift 输出。python3 Tools/compare.py py_out.json swift_out.json"""
import json, sys
from decimal import Decimal, getcontext
getcontext().prec = 60

py, sw = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
# Python Decimal 默认 28 位有效数字，Swift Decimal 38 位；这几个是长除法/迭代结果，按相对误差比较。
APPROX = {"raw_quantity", "sma", "supertrend"}
stats = {"exact": 0, "approx": 0, "mismatch": 0}
bad = []


def same(key, a, b, where):
    if a == b:
        stats["exact"] += 1
        return
    if key in APPROX and a is not None and b is not None:
        da, db = Decimal(a), Decimal(b)
        rel = abs(da - db) / max(abs(da), Decimal("1e-30"))
        if rel < Decimal("1e-20"):
            stats["approx"] += 1
            return
    stats["mismatch"] += 1
    bad.append((where, key, a, b))


def walk(a, b, where, key=None):
    if isinstance(a, dict):
        for k in a:
            walk(a[k], b.get(k) if isinstance(b, dict) else None, f"{where}.{k}", k)
    elif isinstance(a, list):
        if not isinstance(b, list) or len(a) != len(b):
            stats["mismatch"] += 1; bad.append((where, "len", len(a), None if not isinstance(b, list) else len(b))); return
        for i, (x, y) in enumerate(zip(a, b)):
            walk(x, y, f"{where}[{i}]", key)
    else:
        same(key, a, b, where)


for section in ["engine", "suggest", "series", "sign"]:
    walk(py[section], sw[section], section)
passes = sum(1 for e in py["engine"] if e["status"] == "PASS")
print(f"engine cases={len(py['engine'])} (PASS {passes}), suggest={len(py['suggest'])}, series={len(py['series'])}, sign={len(py['sign'])}")
print("fields:", stats)
from collections import Counter
print("engine codes:", dict(Counter(e["reason_code"] for e in py["engine"])))
print("cross types:", dict(Counter(str(x["cross"]["type"]) for x in py["series"])), "divergence:", dict(Counter(str(x["divergence"]["type"]) for x in py["series"])))
print("suggest usable:", dict(Counter(x["usable"] for x in py["suggest"])))
for b in bad[:30]:
    print("MISMATCH", b)
sys.exit(1 if bad else 0)
