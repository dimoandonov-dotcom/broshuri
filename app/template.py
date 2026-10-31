"""
Разчитане на Excel бланката на веригата (Приложение 6Б брошура / 6Г компенсация) без AI.
Доставчиците попълват нашия шаблон, затова колоните се намират по заглавията.
Ако шаблонът не се познае - parse.py пада към Claude.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime


def grid(data: bytes, name: str) -> list[list]:
    if name.lower().endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        sh = wb.sheet_by_index(0)
        out = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                v = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    v = xlrd.xldate.xldate_as_datetime(v, wb.datemode)
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    v = None
                row.append(v)
            out.append(row)
        return out
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]


def _t(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _low(v) -> str:
    return _t(v).lower().replace("ё", "е")


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _t(v).replace("€", "").replace("%", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _pct(v):
    n = _num(v)
    if n is None:
        return None
    return round(n * 100, 2) if 0 < n < 1 else n


def _date(v):
    if isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, date):
        d = v
    else:
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", _t(v))
        if not m:
            return None
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return d if 2020 <= d.year <= 2100 else None


def _barcode(v) -> str | None:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = _t(v)
    if "e" in s.lower():
        return None
    s = re.sub(r"\D", "", s)
    return s if 8 <= len(s) <= 14 else None


COLS = [  # (ключ, проверка на заглавието)
    ("saft", lambda h: h.startswith("saf")),
    ("name", lambda h: h.startswith("артикул")),
    ("barcode", lambda h: h.startswith("баркод")),
    ("base", lambda h: h.startswith("базова")),
    ("dto_pct", lambda h: h.startswith("д.т.о") or h.startswith("дто")),
    ("to_pct", lambda h: h.startswith("т.о") or h == "то %"),
    ("reg", lambda h: h.startswith("регулярна")),
    ("promo", lambda h: h.startswith("промоционална")),
    ("comp", lambda h: h.startswith("компенсация за 1")),
    ("format", lambda h: h.startswith("желан формат")),
]


def _dates_after(row: list, label_idx: int) -> list[date]:
    return [d for d in (_date(v) for v in row[label_idx + 1:]) if d]


def _yes(row: list, i: int):
    for v in row[i + 1:]:
        t = _low(v)
        if t in ("да", "yes"):
            return True
        if t in ("не", "no"):
            return False
    return None


def parse_template(data: bytes, name: str) -> dict | None:
    g = grid(data, name)
    head_i, cols = None, {}
    for i, row in enumerate(g[:60]):
        hs = [_low(v) for v in row]
        if any(h.startswith("артикул") for h in hs) and any(h.startswith("баркод") for h in hs):
            for j, h in enumerate(hs):
                for key, test in COLS:
                    if key not in cols and h and test(h):
                        cols[key] = j
                        break
            head_i = i
            break
    if head_i is None or "name" not in cols:
        return None

    r = {"supplier": "", "kind": "brochure", "brochure_no": None, "promo_from": None, "promo_to": None,
         "price_from": None, "price_to": None, "deadline": None, "region_varna": None, "region_sofia": None,
         "wanted_format": None, "warnings": [], "items": []}
    bad_dates = []
    for i, row in enumerate(g[:head_i]):
        for j, v in enumerate(row):
            t = _low(v)
            if not t:
                continue
            if "компенсация за продадена" in t:
                r["kind"] = "comp"
            if t.startswith("заявка") and not r["brochure_no"]:
                m = re.search(r"№\s*(\d+)", _t(v))
                if m:
                    r["brochure_no"] = m.group(1)
            if "от доставчик" in t and not r["supplier"]:
                rest = [_t(x) for x in row[j + 1:] if _t(x) and not set(_t(x)) <= set(".… ")]
                r["supplier"] = rest[0] if rest else ""
            if "период на промоцията" in t:
                ds = _dates_after(row, j)
                r["promo_from"], r["promo_to"] = (ds + [None, None])[:2]
            if "период на промоционалните доставни" in t or "период на засичане" in t:
                ds = _dates_after(row, j)
                r["price_from"], r["price_to"] = (ds + [None, None])[:2]
            if "краен срок" in t:
                ds = _dates_after(row, j)
                raw = [_t(x) for x in row[j + 1:] if _t(x)]
                r["deadline"] = ds[0] if ds else None
                if raw and not ds:
                    bad_dates.append(f"краен срок „{raw[0]}“")
            if "активация" in t and "варна" in t:
                r["region_varna"] = _yes(row, j)
            if "активация" in t and "софия" in t:
                r["region_sofia"] = _yes(row, j)

    no_bc, no_dto = [], []
    for row in g[head_i + 1:]:
        if not row:
            continue
        cell = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else None  # noqa: E731
        nm = _t(cell("name"))
        low = nm.lower()
        if not nm or low.startswith(("доставчик", "попълва", "общо")):
            if any(_low(v).startswith(("доставчика избира", "доставчикът", "общо дължима")) for v in row):
                break
            continue
        it = {"name": nm, "barcode": _barcode(cell("barcode")), "saft": _t(cell("saft")) or None,
              "base": _num(cell("base")), "to_pct": _pct(cell("to_pct")), "reg": _num(cell("reg")),
              "dto_pct": _pct(cell("dto_pct")), "promo": _num(cell("promo")), "comp": _num(cell("comp"))}
        if all(it[k] is None for k in ("base", "reg", "promo", "comp")) and not it["barcode"]:
            continue
        if not it["barcode"]:
            no_bc.append(nm)
        if r["kind"] == "brochure" and not it["dto_pct"] and (it["reg"] or 0) <= (it["promo"] or 0):
            no_dto.append(nm)
        if not r["wanted_format"] and _t(cell("format")):
            r["wanted_format"] = _t(cell("format"))
        r["items"].append(it)

    if not r["items"]:
        return None
    w = r["warnings"]
    if not r["supplier"]:
        w.append("Доставчикът не е попълнен в бланката.")
    if no_bc:
        w.append(f"Без пълен баркод ({len(no_bc)}): " + ", ".join(no_bc[:5]) + (" …" if len(no_bc) > 5 else ""))
    if no_dto:
        w.append(f"Без отстъпка от доставчика ({len(no_dto)}): " + ", ".join(no_dto[:5]))
    if bad_dates:
        w.append("Грешни дати: " + ", ".join(bad_dates))
    if r["price_to"] and r["promo_to"] and r["price_to"] < r["promo_to"]:
        w.append("Промо доставните цени важат по-кратко от промоцията.")
    return r
