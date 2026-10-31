"""
Данни от MinMaxAI (неговата база, само четене - MINMAX_DATABASE_URL):
продажби 30 дни, реален марж от касата, наличност и как се е продавал артикулът в минали брошури.
"""
from __future__ import annotations

import os
from datetime import timedelta

from sqlalchemy import create_engine, text

_eng = None


def engine():
    global _eng
    url = os.getenv("MINMAX_DATABASE_URL", "")
    if not url:
        return None
    if _eng is None:
        url = url.replace("postgres://", "postgresql+psycopg2://", 1).replace("postgresql://", "postgresql+psycopg2://", 1)
        _eng = create_engine(url, pool_pre_ping=True, pool_size=2)
    return _eng


def info(codes: list[int]) -> tuple[dict[int, dict], list[str]]:
    eng = engine()
    codes = sorted({int(c) for c in codes if c})
    if not eng or not codes:
        return {}, []
    out: dict[int, dict] = {c: {} for c in codes}
    try:
        with eng.connect() as c:
            last = c.execute(text("SELECT MAX(day) FROM sa_sales")).scalar()
            if not last:
                return {}, ["MinMaxAI още няма продажби."]
            for code, q, rev, cost, st in c.execute(text("""
                    SELECT code, SUM(qty), SUM(rev), SUM(cost), COUNT(DISTINCT store_id) FILTER (WHERE qty > 0)
                    FROM sa_sales WHERE day > :d AND code = ANY(:c) GROUP BY code"""),
                    {"d": last - timedelta(days=30), "c": codes}):
                o = out[code]
                o["mm_sales30"] = int(round(float(q or 0)))
                o["mm_stores"] = int(st or 0)
                if rev and float(rev) > 0:
                    o["margin30"] = round(100 * (float(rev) - float(cost or 0)) / float(rev), 1)
            for code, q in c.execute(text("SELECT code, SUM(qty) FROM sa_stock WHERE code = ANY(:c) GROUP BY code"),
                                     {"c": codes}):
                out[code]["stock"] = int(round(float(q or 0)))
            # последната брошура за всеки артикул (тип 1) и ефектът ѝ: продажби/ден по време ÷ 14 дни преди
            promos = {}
            for code, s, e, disc, price in c.execute(text("""
                    SELECT DISTINCT ON (code) code, start_day, end_day, discount, price FROM sa_promo
                    WHERE code = ANY(:c) AND COALESCE(tp, 1) = 1 AND start_day <= :l
                    ORDER BY code, (end_day - start_day >= 3) DESC, start_day DESC"""), {"c": codes, "l": last}):
                promos[code] = (s, e, disc, price)
            for code, (s, e, disc, price) in promos.items():
                pe = min(e, last)
                days = (pe - s).days + 1
                q = c.execute(text("""SELECT
                        SUM(CASE WHEN day < :s THEN qty ELSE 0 END), SUM(CASE WHEN day >= :s THEN qty ELSE 0 END)
                        FROM sa_sales WHERE code = :c AND day BETWEEN :b AND :e"""),
                              {"c": code, "s": s, "b": s - timedelta(days=14), "e": pe}).first()
                before, during = float(q[0] or 0) / 14, (float(q[1] or 0) / days if days > 0 else 0)
                out[code]["last_promo"] = (f"{s:%d.%m}–{e:%d.%m.%y}"
                                           + (f", -{float(disc):.0f}%" if disc else "")
                                           + (f", {float(price):.2f} €" if price else ""))
                out[code]["uplift"] = round(during / before, 1) if before > 0 and days > 0 else None
    except Exception as e:  # noqa: BLE001
        return {}, [f"MinMaxAI: {e}"]
    return out, []
