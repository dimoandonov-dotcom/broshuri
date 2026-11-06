"""
Списък КЗП (артикули под наблюдение) - Excel от папка КЗП в Drive / качен от офиса.
Правила за брошурата:
 1. Артикулът в КЗП свети в различен цвят.
 2. Маржът на промо цената не може да е по-голям от регулярния марж (може = или <).
 3. Ако артикулът е бил на активация в последните 30 дни (15 за кисело/прясно мляко и хляб)
    преди началото на брошурата - не може да влезе в брошурата.
"""
from __future__ import annotations

import io
import re
from datetime import date, timedelta

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS kzp (
  code INT PRIMARY KEY, name TEXT, category TEXT, grp TEXT, firm TEXT,
  perishable BOOLEAN NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS kzp_meta (id INT PRIMARY KEY, file_name TEXT, n INT, updated_by TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""
PERISHABLE = re.compile(r"кисело мляко|прясно мляко|хляб", re.I)
DAYS_LONG, DAYS_SHORT = 30, 15


def init():
    for st in SCHEMA.split(";"):
        if st.strip():
            db.q(st)


def load_xlsx(data: bytes, file_name: str, user: str = "") -> int:
    import openpyxl
    ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True).worksheets[0]
    rows, grp = {}, None
    for r in ws.iter_rows(values_only=True):
        a = r[0] if r else None
        name = str(r[1]).strip() if len(r) > 1 and r[1] else ""
        if a is None and name:
            grp = name
            continue
        try:
            code = int(float(a))
        except (TypeError, ValueError):
            continue
        cat = str(r[2]).strip() if len(r) > 2 and r[2] else ""
        firm = str(r[3]).strip() if len(r) > 3 and r[3] else ""
        rows[code] = {"code": code, "name": name, "category": cat, "grp": grp, "firm": firm,
                      "perishable": bool(PERISHABLE.search(cat or name))}
    if not rows:
        raise ValueError("Не намерих артикули - първата колона трябва да е „Мат. №“.")
    with db.engine.begin() as c:
        from sqlalchemy import text
        c.execute(text("DELETE FROM kzp"))
        c.execute(text("""INSERT INTO kzp (code, name, category, grp, firm, perishable)
                          VALUES (:code, :name, :category, :grp, :firm, :perishable)"""), list(rows.values()))
        c.execute(text("""INSERT INTO kzp_meta (id, file_name, n, updated_by) VALUES (1, :f, :n, :u)
                          ON CONFLICT (id) DO UPDATE SET file_name=:f, n=:n, updated_by=:u, updated_at=now()"""),
                  {"f": file_name, "n": len(rows), "u": user})
    return len(rows)


def meta() -> dict | None:
    return db.one("SELECT file_name, n, updated_by, updated_at FROM kzp_meta WHERE id=1")


def lookup(codes: list[int]) -> dict[int, dict]:
    codes = [int(c) for c in codes if c]
    if not codes:
        return {}
    return {r["code"]: r for r in db.rows("SELECT * FROM kzp WHERE code = ANY(:c)", c=codes)}


def activations(codes_days: dict[int, int], start: date) -> dict[int, list[dict]]:
    """Активации (всякакви промоции от Мистрал през MinMaxAI) в прозореца [start - дни, start)."""
    from sqlalchemy import text
    from .minmax import engine
    eng = engine()
    if not eng or not codes_days:
        return {}
    out: dict[int, list[dict]] = {}
    far = start - timedelta(days=max(codes_days.values()))
    with eng.connect() as c:
        for code, s, e, disc, price, tp in c.execute(text("""
                SELECT code, start_day, end_day, discount, price, COALESCE(tp, 0) FROM sa_promo
                WHERE code = ANY(:c) AND end_day >= :far AND start_day < :st
                ORDER BY start_day DESC"""), {"c": list(codes_days), "far": far, "st": start}):
            if e >= start - timedelta(days=codes_days[code]):
                out.setdefault(code, []).append({"start": s, "end": e, "discount": float(disc) if disc else None,
                                                 "price": float(price) if price else None, "tp": int(tp)})
    return out
