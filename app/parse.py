"""
Разчитане на бланка от доставчик (Excel, PDF или снимка) с Claude API.
Връща заглавната част (доставчик, периоди, региони) и редовете с артикулите.
Excel се превръща в текст ние (така баркодовете не стават 3.80023E+12);
PDF и снимки отиват директно към модела.
"""
from __future__ import annotations

import base64
import io
import json
import os
from datetime import date, datetime

import anthropic

MODEL = os.getenv("PARSE_MODEL", "claude-sonnet-5-5")

TOOL = {
    "name": "save_form",
    "description": "Записва разчетената бланка.",
    "input_schema": {
        "type": "object",
        "properties": {
            "supplier": {"type": "string", "description": "Доставчик (фирма). Празно, ако не е попълнен."},
            "kind": {"type": "string", "enum": ["brochure", "comp"],
                     "description": "brochure = участие в рекламна брошура; comp = компенсация за продадена бройка"},
            "brochure_no": {"type": "string"},
            "promo_from": {"type": "string", "description": "YYYY-MM-DD"},
            "promo_to": {"type": "string"},
            "price_from": {"type": "string", "description": "период на промо доставните цени / на компенсацията"},
            "price_to": {"type": "string"},
            "deadline": {"type": "string"},
            "region_varna": {"type": ["boolean", "null"]},
            "region_sofia": {"type": ["boolean", "null"]},
            "wanted_format": {"type": "string", "description": "Желан формат на карето, ако е общ"},
            "warnings": {"type": "array", "items": {"type": "string"},
                         "description": "Нередности: липсващи баркодове, грешни дати, липсващ доставчик, без отстъпка и т.н. На български."},
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"},
                "barcode": {"type": "string", "description": "Само цифри. Празно, ако липсва или е съкратен."},
                "saft": {"type": "string"},
                "base": {"type": ["number", "null"], "description": "Базова цена с ДДС"},
                "to_pct": {"type": ["number", "null"]},
                "reg": {"type": ["number", "null"], "description": "Регулярна доставна цена с ДДС"},
                "dto_pct": {"type": ["number", "null"]},
                "promo": {"type": ["number", "null"], "description": "Промоционална доставна цена с ДДС"},
                "comp": {"type": ["number", "null"], "description": "Компенсация за 1 брой с ДДС (само при kind=comp)"},
                "wanted_format": {"type": "string"},
            }, "required": ["name"]}},
        },
        "required": ["supplier", "kind", "items", "warnings"],
    },
}

PROMPT = """Това е бланка „Заявка за участие в РЕКЛАМНА БРОШУРА“ или „Заявка за КОМПЕНСАЦИЯ ЗА ПРОДАДЕНА БРОЙКА“
от доставчик към верига магазини. Извлечи заглавната част и ВСИЧКИ редове с артикули (празните редове пропусни).
Правила:
- Цените са в евро с ДДС. Процентите като числа (10.00% -> 10; ако в Excel е записано 0.07, това е 7).
- Баркод: само ако е пълен (8-14 цифри). Ако е във вид 3.80023E+12 или е съкратен - остави празно и го запиши в warnings.
- Не измисляй стойности. Ако нещо липсва - null / празно.
- Ръкописни бележки върху снимка игнорирай (те са на купувача), освен ако са единственият източник на баркод.
- Дати във формат YYYY-MM-DD. Ако датата е явно грешна (напр. 1.7.1900 или 10.13.2026), запиши я в warnings.
- В warnings отбележи и: липсващ доставчик, редове без отстъпка (Д.Т.О. празно), съкратени периоди."""


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer() and abs(v) >= 1e6:
            return str(int(v))
        return repr(round(v, 6))
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    return str(v).replace("\t", " ").replace("\n", " ").strip()


def excel_to_text(data: bytes, name: str) -> str:
    out = []
    if name.lower().endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        for sh in wb.sheets():
            out.append(f"### Лист: {sh.name}")
            for r in range(sh.nrows):
                vals = []
                for c in range(sh.ncols):
                    cell = sh.cell(r, c)
                    v = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        v = xlrd.xldate.xldate_as_datetime(v, wb.datemode)
                    vals.append(_cell(v))
                if any(vals):
                    out.append("\t".join(vals))
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        for ws in wb.worksheets:
            out.append(f"### Лист: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                vals = [_cell(v) for v in row]
                while vals and not vals[-1]:
                    vals.pop()
                if any(vals):
                    out.append("\t".join(vals))
    return "\n".join(out)[:120_000]


def _image_block(data: bytes) -> dict:
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    im.thumbnail((2400, 2400))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def file_kind(name: str, ctype: str = "") -> str:
    n = name.lower()
    if n.endswith((".xlsx", ".xlsm", ".xls")):
        return "excel"
    if n.endswith(".pdf") or "pdf" in ctype:
        return "pdf"
    if n.endswith((".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp")) or ctype.startswith("image/"):
        return "image"
    return ""


def parse(data: bytes, name: str, ctype: str = "") -> dict:
    kind = file_kind(name, ctype)
    if not kind:
        raise ValueError("Непознат файл. Качи Excel, PDF или снимка.")
    if kind == "excel":
        content = [{"type": "text", "text": "Съдържание на Excel файла (колоните са разделени с табулация):\n\n"
                    + excel_to_text(data, name)}]
    elif kind == "pdf":
        content = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                   "data": base64.b64encode(data).decode()}}]
    else:
        content = [_image_block(data)]
    content.append({"type": "text", "text": PROMPT})
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL, max_tokens=8000, tools=[TOOL],
        tool_choice={"type": "tool", "name": "save_form"},
        messages=[{"role": "user", "content": content}],
    )
    for b in msg.content:
        if b.type == "tool_use":
            return clean(b.input)
    raise ValueError("Бланката не можа да се разчете.")


def _d(v):
    try:
        d = date.fromisoformat(str(v)[:10])
        return d if 2020 <= d.year <= 2100 else None
    except (TypeError, ValueError):
        return None


def _n(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def clean(r: dict) -> dict:
    items = []
    for it in r.get("items") or []:
        raw = str(it.get("barcode") or "")
        bc = "" if "e" in raw.lower() else "".join(ch for ch in raw if ch.isdigit())
        items.append({
            "name": (it.get("name") or "").strip(),
            "barcode": bc if 8 <= len(bc) <= 14 else None,
            "saft": (it.get("saft") or "").strip() or None,
            **{k: _n(it.get(k)) for k in ("base", "to_pct", "reg", "dto_pct", "promo", "comp")},
        })
        for k in ("to_pct", "dto_pct"):
            if items[-1][k] is not None and 0 < items[-1][k] < 1:
                items[-1][k] = round(items[-1][k] * 100, 2)
    return {
        "supplier": (r.get("supplier") or "").strip(),
        "kind": r.get("kind") if r.get("kind") in ("brochure", "comp") else "brochure",
        "brochure_no": (r.get("brochure_no") or "").strip() or None,
        **{k: _d(r.get(k)) for k in ("promo_from", "promo_to", "price_from", "price_to", "deadline")},
        "region_varna": r.get("region_varna"), "region_sofia": r.get("region_sofia"),
        "wanted_format": (r.get("wanted_format") or "").strip() or None,
        "warnings": [w for w in (r.get("warnings") or []) if w],
        "items": [i for i in items if i["name"]],
    }


if __name__ == "__main__":
    import sys
    p = sys.argv[1]
    print(json.dumps(parse(open(p, "rb").read(), p), ensure_ascii=False, default=str, indent=1))
