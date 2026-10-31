"""
Мистрал (MS SQL, база MistralAnalytics) - само четене. Същите настройки като MinMaxAI:
MSSQL_HOST/PORT/USER/PASSWORD/DB (Варна) и SOFIA_MSSQL_* (София).

За всеки артикул от бланката: баркод -> наш код, име, рафтова цена (основната ценова листа),
средна доставна цена и продажби в бройки за последните 30 пълни дни.
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import date, timedelta
from zoneinfo import ZoneInfo

SOFIA_TZ = ZoneInfo("Europe/Sofia")
SALES_DAYS = int(os.getenv("SALES_DAYS", "30"))


def regions() -> list[str]:
    out = []
    if os.getenv("MSSQL_HOST") and os.getenv("MSSQL_PASSWORD"):
        out.append("varna")
    if os.getenv("SOFIA_MSSQL_HOST") and os.getenv("SOFIA_MSSQL_PASSWORD"):
        out.append("sofia")
    return out


def connect(region: str = "varna"):
    import pymssql
    p = "SOFIA_MSSQL_" if region == "sofia" else "MSSQL_"
    return pymssql.connect(
        server=os.getenv(p + "HOST", ""), port=int(os.getenv(p + "PORT", "1433")),
        user=os.getenv(p + "USER", ""), password=os.getenv(p + "PASSWORD", ""),
        database=os.getenv(p + "DB", "MistralAnalytics"),
        login_timeout=15, timeout=120, charset="UTF-8", as_dict=True,
    )


def _cols(cur, table: str) -> list[str]:
    cur.execute("SELECT COLUMN_NAME AS c FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = %s", (table,))
    return [r["c"] for r in cur.fetchall()]


def _ints(xs) -> str:
    return ",".join(str(int(x)) for x in xs)


def _codes_by_barcode(cur, barcodes: list[str]) -> dict[str, int]:
    cols = _cols(cur, "BARCODE")
    bc_col = next((c for c in cols if c.upper() == "BARCODE"), None) \
        or next((c for c in cols if "BARCODE" in c.upper() or c.upper() in ("CODE", "EAN")), None)
    code_col = next((c for c in cols if c.upper() == "MATERIALCODE"), None)
    if not bc_col or not code_col:
        raise RuntimeError(f"Таблица BARCODE: не намирам колоните ({', '.join(cols)})")
    out: dict[str, int] = {}
    for j in range(0, len(barcodes), 500):
        part = barcodes[j:j + 500]
        marks = ",".join(["%s"] * len(part))
        cur.execute(f"SELECT DISTINCT [{bc_col}] AS bc, [{code_col}] AS code FROM BARCODE WITH (NOLOCK) "
                    f"WHERE [{bc_col}] IN ({marks})", tuple(part))
        for r in cur.fetchall():
            out.setdefault(str(r["bc"]).strip(), int(r["code"]))
    return out


def _info(cur, codes: list[int]) -> dict[int, dict]:
    if not codes:
        return {}
    cl = _ints(codes)
    info: dict[int, dict] = {c: {"prices": []} for c in codes}
    cur.execute(f"""SELECT MATERIALCODE AS code, MAX(MATERIAL) AS name, MAX(AVGDELIVERYPRICE) AS avgp
                    FROM MATERIAL WITH (NOLOCK) WHERE MATERIALCODE IN ({cl}) GROUP BY MATERIALCODE""")
    for r in cur.fetchall():
        info[int(r["code"])].update(name=r["name"], avg_delivery=float(r["avgp"] or 0) or None)
    cur.execute(f"""SELECT pl.MATERIALCODE AS code, pl.PRICE AS price
                    FROM PRICELIST pl WITH (NOLOCK)
                    JOIN PRICELISTNAME pn ON pn.LOCATIONID = pl.LOCATIONID AND pn.ID = pl.PRICELISTID
                    WHERE pn.PRICELISTTYPE = 0 AND pl.MATERIALCODE IN ({cl}) AND pl.PRICE > 0""")
    for r in cur.fetchall():
        info[int(r["code"])]["prices"].append(round(float(r["price"]), 2))
    today = __import__("datetime").datetime.now(SOFIA_TZ).date()
    d_from = today - timedelta(days=SALES_DAYS)
    cur.execute(f"""SELECT c.MATERIALCODE AS code, SUM(c.QTY) AS qty
                    FROM SALE s WITH (NOLOCK)
                    JOIN SALECONTENT c WITH (NOLOCK) ON c.LOCATIONID = s.LOCATIONID AND c.NUM = s.NUM
                    WHERE s.SALEDATE >= %s AND s.SALEDATE < %s AND c.MATERIALCODE IN ({cl})
                    GROUP BY c.MATERIALCODE""", (d_from.isoformat(), today.isoformat()))
    for r in cur.fetchall():
        info[int(r["code"])]["sales"] = int(round(float(r["qty"] or 0)))
    return info


def enrich(items: list[dict]) -> tuple[list[dict], list[str]]:
    """items: [{id, barcode, code}] -> [{id, code, m_name, shelf, avg_delivery, sales30_*}], бележки."""
    regs = regions()
    if not regs:
        return [], ["Мистрал не е настроен (липсват MSSQL_* променливи)."]
    notes: list[str] = []
    res = {it["id"]: {"id": it["id"], "code": it.get("code")} for it in items}
    for region in regs:
        try:
            with connect(region) as conn:
                cur = conn.cursor()
                need = [it["barcode"] for it in items if it.get("barcode") and not res[it["id"]].get("code")]
                if need:
                    found = _codes_by_barcode(cur, sorted(set(need)))
                    for it in items:
                        if it.get("barcode") in found and not res[it["id"]].get("code"):
                            res[it["id"]]["code"] = found[it["barcode"]]
                codes = sorted({r["code"] for r in res.values() if r.get("code")})
                info = _info(cur, codes)
                for r in res.values():
                    i = info.get(r.get("code") or -1)
                    if not i:
                        continue
                    r.setdefault("m_name", i.get("name"))
                    if i["prices"] and region == "varna" or (i["prices"] and not r.get("shelf")):
                        r["shelf"] = Counter(i["prices"]).most_common(1)[0][0]
                    r.setdefault("avg_delivery", i.get("avg_delivery"))
                    r[f"sales30_{region}"] = i.get("sales", 0)
        except Exception as e:  # noqa: BLE001
            notes.append(f"Мистрал {region}: {e}")
    out = []
    for r in res.values():
        if r.get("code"):
            r["sales30"] = (r.get("sales30_varna") or 0) + (r.get("sales30_sofia") or 0)
        out.append(r)
    miss = sum(1 for r in out if not r.get("code"))
    if miss:
        notes.append(f"{miss} арт. без код в Мистрал - въведи кода ръчно.")
    return out, notes
