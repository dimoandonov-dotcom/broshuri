"""PostgreSQL: бланки (forms), редове (items), потребители и прикачени файлове."""
from __future__ import annotations

import os

from sqlalchemy import create_engine, text

URL = os.getenv("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/broshuri")
if URL.startswith("postgres://"):
    URL = URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif URL.startswith("postgresql://"):
    URL = URL.replace("postgresql://", "postgresql+psycopg2://", 1)

engine = create_engine(URL, pool_pre_ping=True, future=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS forms (
  id SERIAL PRIMARY KEY,
  supplier TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'brochure',          -- brochure | comp (компенсация за продадена бройка)
  brochure_no TEXT,
  promo_from DATE, promo_to DATE, price_from DATE, price_to DATE,
  deadline DATE,
  region_varna BOOLEAN, region_sofia BOOLEAN,
  wanted_format TEXT,
  status TEXT NOT NULL DEFAULT 'pending',          -- pending | approved | counter | rejected
  fmt TEXT, note TEXT,
  source TEXT NOT NULL DEFAULT 'upload',          -- upload | email
  source_from TEXT, file_name TEXT, file_type TEXT, file_data BYTEA,
  warnings TEXT,
  created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_by TEXT, decided_at TIMESTAMPTZ,
  sent_at TIMESTAMPTZ, sent_by TEXT
);
CREATE TABLE IF NOT EXISTS items (
  id SERIAL PRIMARY KEY,
  form_id INT NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
  pos INT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  barcode TEXT, saft TEXT,
  base NUMERIC(12,4), to_pct NUMERIC(6,2), reg NUMERIC(12,4), dto_pct NUMERIC(6,2),
  promo NUMERIC(12,4), comp NUMERIC(12,4),
  code INT, m_name TEXT,
  shelf NUMERIC(12,2), avg_delivery NUMERIC(12,4),
  sales30 INT, sales30_varna INT, sales30_sofia INT,
  approved NUMERIC(12,2), item_note TEXT,
  enriched_at TIMESTAMPTZ
);
ALTER TABLE items ADD COLUMN IF NOT EXISTS margin30 NUMERIC(6,1);
ALTER TABLE items ADD COLUMN IF NOT EXISTS stock INT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS stores INT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS last_promo TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS uplift NUMERIC(6,1);
ALTER TABLE items ADD COLUMN IF NOT EXISTS reg_src TEXT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS vat NUMERIC(4,1);
ALTER TABLE items ADD COLUMN IF NOT EXISTS m_delivery NUMERIC(12,4);
ALTER TABLE items ADD COLUMN IF NOT EXISTS m_delivery_date DATE;
CREATE TABLE IF NOT EXISTS supplier_prices (
  supplier TEXT NOT NULL, code INT NOT NULL, reg NUMERIC(12,4) NOT NULL,
  source TEXT, updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (supplier, code));
ALTER TABLE supplier_prices ADD COLUMN IF NOT EXISTS base NUMERIC(12,4);
ALTER TABLE supplier_prices ADD COLUMN IF NOT EXISTS to_pct NUMERIC(6,2);
CREATE INDEX IF NOT EXISTS items_form ON items(form_id, pos);
CREATE TABLE IF NOT EXISTS mail_seen (msg_id TEXT PRIMARY KEY, seen_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""


def init():
    with engine.begin() as c:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                c.execute(text(stmt))


def q(sql: str, **kw):
    with engine.begin() as c:
        return c.execute(text(sql), kw)


def rows(sql: str, **kw) -> list[dict]:
    with engine.begin() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), kw)]


def one(sql: str, **kw) -> dict | None:
    r = rows(sql, **kw)
    return r[0] if r else None
