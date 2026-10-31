"""
Брошури - заявки от доставчици за рекламна брошура / компенсация.
Офисът качва бланката (или тя идва по имейл) -> Claude я разчита -> Мистрал добавя код,
рафтова цена и продажби -> Асен решава от телефона -> офисът сваля отговора и го праща.

Променливи: DATABASE_URL, ANTHROPIC_API_KEY, SECRET_KEY, USERS ("asen:1234:approver,ofis:5678:office"),
MSSQL_* / SOFIA_MSSQL_*, IMAP_HOST/USER/PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_IDS, PUBLIC_URL.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import db, extras, mistral, parse

app = FastAPI(title="Брошури")
SECRET = os.getenv("SECRET_KEY", "dev-secret")
COOKIE = "br_session"
MAIL_EVERY = int(os.getenv("MAIL_EVERY_MIN", "5"))


# ---------- потребители ----------
def users() -> dict[str, dict]:
    out = {}
    for part in os.getenv("USERS", "admin:0000:admin").split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) >= 3 and bits[0]:
            out[bits[0].lower()] = {"pin": bits[1], "role": bits[2]}
    return out


def _sig(u: str) -> str:
    pin = users().get(u, {}).get("pin", "")
    return hmac.new(SECRET.encode(), f"{u}|{pin}".encode(), hashlib.sha256).hexdigest()[:40]


def me(request: Request) -> dict:
    c = request.cookies.get(COOKIE, "")
    u, _, s = c.partition(".")
    if u in users() and s and hmac.compare_digest(s, _sig(u)):
        return {"user": u, "role": users()[u]["role"]}
    raise HTTPException(401, "Влез отново.")


def need(*roles):
    def dep(user: dict = Depends(me)):
        if user["role"] != "admin" and user["role"] not in roles:
            raise HTTPException(403, "Нямаш права за това.")
        return user
    return dep


class Login(BaseModel):
    user: str
    pin: str


@app.post("/api/login")
def login(p: Login):
    u = p.user.strip().lower()
    if u not in users() or not hmac.compare_digest(p.pin.strip(), users()[u]["pin"]):
        raise HTTPException(401, "Грешно име или ПИН.")
    r = JSONResponse({"user": u, "role": users()[u]["role"]})
    r.set_cookie(COOKIE, f"{u}.{_sig(u)}", max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax",
                 secure=os.getenv("PUBLIC_URL", "").startswith("https"))
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/api/me")
def whoami(user: dict = Depends(me)):
    return user


# ---------- приемане на бланка ----------
ITEM_COLS = ("name", "barcode", "saft", "base", "to_pct", "reg", "dto_pct", "promo", "comp")


def enrich_form(form_id: int) -> list[str]:
    items = db.rows("SELECT id, barcode, code FROM items WHERE form_id=:f ORDER BY pos", f=form_id)
    res, notes = mistral.enrich(items)
    from . import minmax
    mm, mm_notes = minmax.info([r["code"] for r in res if r.get("code")])
    notes += mm_notes
    for r in res:
        x = mm.get(r.get("code") or -1, {})
        if x.get("mm_sales30") is not None:
            r["sales30"] = x["mm_sales30"]
        db.q("""UPDATE items SET margin30=:a, stock=:b, stores=:c, last_promo=:d, uplift=:e WHERE id=:id""",
             id=r["id"], a=x.get("margin30"), b=x.get("stock"), c=x.get("mm_stores"),
             d=x.get("last_promo"), e=x.get("uplift"))
        db.q("""UPDATE items SET barcode=COALESCE(barcode, :bc),
                reg=CASE WHEN reg IS NULL AND (SELECT source FROM forms WHERE id=items.form_id)='office' THEN :ld ELSE reg END
                WHERE id=:id""", id=r["id"], bc=r.get("barcode"), ld=r.get("last_delivery"))
        db.q("""UPDATE items SET code=:code, m_name=COALESCE(:m_name, m_name), shelf=COALESCE(:shelf, shelf),
                avg_delivery=:avg_delivery, sales30=:sales30, sales30_varna=:sv, sales30_sofia=:ss,
                enriched_at=now() WHERE id=:id""",
             id=r["id"], code=r.get("code"), m_name=r.get("m_name"), shelf=r.get("shelf"),
             avg_delivery=r.get("avg_delivery"), sales30=r.get("sales30"),
             sv=r.get("sales30_varna"), ss=r.get("sales30_sofia"))
    return notes


def ingest(data: bytes, name: str, ctype: str, source: str = "upload", source_from: str | None = None,
           user: str = "") -> int:
    p = parse.parse(data, name, ctype)
    fid = db.one("""INSERT INTO forms (supplier, kind, brochure_no, promo_from, promo_to, price_from, price_to,
            deadline, region_varna, region_sofia, wanted_format, warnings, source, source_from,
            file_name, file_type, file_data, created_by)
        VALUES (:supplier, :kind, :brochure_no, :promo_from, :promo_to, :price_from, :price_to, :deadline,
            :region_varna, :region_sofia, :wanted_format, :warnings, :source, :source_from,
            :file_name, :file_type, :file_data, :created_by) RETURNING id""",
                 **{k: p[k] for k in ("supplier", "kind", "brochure_no", "promo_from", "promo_to", "price_from",
                                      "price_to", "deadline", "region_varna", "region_sofia", "wanted_format")},
                 warnings="\n".join(p["warnings"]), source=source, source_from=source_from,
                 file_name=name, file_type=ctype, file_data=data, created_by=user)["id"]
    for i, it in enumerate(p["items"], 1):
        db.q(f"""INSERT INTO items (form_id, pos, {', '.join(ITEM_COLS)})
                 VALUES (:f, :pos, {', '.join(':' + c for c in ITEM_COLS)})""", f=fid, pos=i, **it)
    notes = enrich_form(fid)
    if notes:
        w = "\n".join(filter(None, ["\n".join(p["warnings"]), *notes]))
        db.q("UPDATE forms SET warnings=:w WHERE id=:id", w=w, id=fid)
    url = f"{extras.PUBLIC_URL}/#f{fid}" if extras.PUBLIC_URL else ""
    extras.telegram(f"Нова бланка за брошура: {p['supplier'] or 'доставчик?'} - {len(p['items'])} арт.\n{url}")
    return fid


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), user: dict = Depends(need("office"))):
    out, errs = [], []
    for f in files:
        data = await f.read()
        try:
            out.append(await run_in_threadpool(ingest, data, f.filename or "file", f.content_type or "",
                                               "upload", None, user["user"]))
        except Exception as e:  # noqa: BLE001
            errs.append(f"{f.filename}: {e}")
    return {"ids": out, "errors": errs}


# ---------- нова брошура от офиса: доставчик -> артикули ----------
@app.get("/api/suppliers")
def suppliers(user: dict = Depends(need("office"))):
    from . import minmax
    return minmax.suppliers()


@app.get("/api/suppliers/articles")
def supplier_articles(name: str, user: dict = Depends(need("office"))):
    from . import minmax
    return minmax.articles(name)


@app.get("/api/articles/search")
async def article_search(q: str, supplier: str | None = None, user: dict = Depends(need("office"))):
    from . import minmax
    return await run_in_threadpool(minmax.search, q, supplier)


@app.get("/api/today")
def today_phrase():
    from zoneinfo import ZoneInfo
    d = datetime.now(ZoneInfo("Europe/Sofia")).date()
    try:
        from .phrases import today_phrase as tp
        return {"phrase": tp(d), "logo": os.getenv("LOGO_URL", "")}
    except Exception:  # noqa: BLE001
        return {"phrase": "Всяка цена в брошурата е обещание към квартала. Нека е честно и печелившо.",
                "logo": os.getenv("LOGO_URL", "")}


class NewForm(BaseModel):
    supplier: str
    codes: list[int]
    brochure_no: str | None = None
    promo_from: str | None = None
    promo_to: str | None = None
    price_from: str | None = None
    price_to: str | None = None
    region_varna: bool = True
    region_sofia: bool = True


@app.post("/api/forms/new")
async def new_form(p: NewForm, user: dict = Depends(need("office"))):
    from . import minmax
    if not p.codes:
        raise HTTPException(400, "Избери поне един артикул.")
    names = minmax.names(p.codes)
    fid = db.one("""INSERT INTO forms (supplier, kind, brochure_no, promo_from, promo_to, price_from, price_to,
            region_varna, region_sofia, source, created_by)
        VALUES (:s, 'brochure', :b, :pf, :pt, :rf, :rt, :v, :so, 'office', :u) RETURNING id""",
                 s=p.supplier.strip(), b=p.brochure_no, pf=p.promo_from or None, pt=p.promo_to or None,
                 rf=p.price_from or None, rt=p.price_to or None, v=p.region_varna, so=p.region_sofia,
                 u=user["user"])["id"]
    for i, c in enumerate(p.codes, 1):
        db.q("INSERT INTO items (form_id, pos, name, code) VALUES (:f, :p, :n, :c)",
             f=fid, p=i, n=names.get(c, str(c)), c=c)
    notes = await run_in_threadpool(enrich_form, fid)
    if notes:
        db.q("UPDATE forms SET warnings=:w WHERE id=:id", w="\n".join(notes), id=fid)
    url = f"{extras.PUBLIC_URL}/#f{fid}" if extras.PUBLIC_URL else ""
    extras.telegram(f"Брошура за цени: {p.supplier} - {len(p.codes)} арт. Чака твоите цени.\n{url}")
    return {"id": fid}


# ---------- четене ----------
def _num(v):
    return float(v) if v is not None else None


MIN_MARGIN = float(os.getenv("MIN_MARGIN", "20"))


def _item_calc(it: dict, kind: str) -> dict:
    reg, promo, comp = _num(it["reg"]), _num(it["promo"]), _num(it["comp"])
    cost = (reg - comp) if (kind == "comp" and reg is not None and comp is not None) else promo
    app_, shelf = _num(it["approved"]), _num(it["shelf"])
    sup_gives = comp if kind == "comp" else ((reg - promo) if reg is not None and promo is not None else None)
    it["cost"] = cost
    it["margin"] = round((app_ - cost) / app_ * 100, 1) if app_ and cost is not None else None
    it["reg_margin"] = round((shelf - reg) / shelf * 100, 1) if shelf and reg is not None else None
    it["off_pct"] = round((1 - app_ / shelf) * 100) if app_ and shelf else None
    it["funded"] = round((shelf - app_) - sup_gives, 4) if app_ and shelf and sup_gives is not None else None
    # каква промо доставна цена ни трябва, за да имаме MIN_MARGIN при тази рафтова цена
    it["need_delivery"] = round(app_ * (1 - MIN_MARGIN / 100), 4) if app_ else None
    it["need_dto"] = round((1 - it["need_delivery"] / reg) * 100, 1) if app_ and reg else None
    it["min_margin"] = MIN_MARGIN
    return it


SUMMARY = """SELECT f.id, f.supplier, f.kind, f.brochure_no, f.promo_from, f.promo_to, f.status, f.source,
    f.created_at, f.decided_by, f.decided_at, f.sent_at, f.warnings IS NOT NULL AND f.warnings <> '' AS has_warn,
    COUNT(i.id) AS n_items, COUNT(i.approved) AS n_approved
  FROM forms f LEFT JOIN items i ON i.form_id = f.id"""


@app.get("/api/forms")
def list_forms(user: dict = Depends(me)):
    return db.rows(SUMMARY + " GROUP BY f.id ORDER BY (f.status='pending') DESC, f.created_at DESC LIMIT 300")


@app.get("/api/forms/{fid}")
def get_form(fid: int, user: dict = Depends(me)):
    f = db.one("SELECT * FROM forms WHERE id=:id", id=fid)
    if not f:
        raise HTTPException(404, "Няма такава бланка.")
    f.pop("file_data", None)
    f["items"] = [_item_calc(it, f["kind"]) for it in
                  db.rows("SELECT * FROM items WHERE form_id=:f ORDER BY pos", f=fid)]
    return f


# ---------- решения ----------
class Decision(BaseModel):
    status: str | None = None
    fmt: str | None = None
    note: str | None = None
    supplier: str | None = None


@app.post("/api/forms/{fid}")
def update_form(fid: int, p: Decision, user: dict = Depends(me)):
    if p.status is not None:
        if user["role"] not in ("approver", "admin"):
            raise HTTPException(403, "Само Асен решава.")
        if p.status not in extras.ST:
            raise HTTPException(400, "Непознато решение.")
        db.q("UPDATE forms SET status=:s, decided_by=:u, decided_at=now() WHERE id=:id",
             s=p.status, u=user["user"], id=fid)
    for k in ("fmt", "note", "supplier"):
        v = getattr(p, k)
        if v is not None:
            db.q(f"UPDATE forms SET {k}=:v WHERE id=:id", v=v.strip(), id=fid)
    return get_form(fid, user)


class ItemPatch(BaseModel):
    approved: float | None = None
    shelf: float | None = None
    code: int | None = None
    sales30: int | None = None
    item_note: str | None = None
    clear_approved: bool = False


@app.post("/api/items/{iid}")
def update_item(iid: int, p: ItemPatch, user: dict = Depends(me)):
    it = db.one("SELECT form_id FROM items WHERE id=:id", id=iid)
    if not it:
        raise HTTPException(404, "Няма такъв ред.")
    if (p.approved is not None or p.clear_approved) and user["role"] not in ("approver", "admin"):
        raise HTTPException(403, "Само Асен определя цената.")
    if p.clear_approved:
        db.q("UPDATE items SET approved=NULL WHERE id=:id", id=iid)
    for k in ("approved", "shelf", "code", "sales30", "item_note"):
        v = getattr(p, k)
        if v is not None:
            db.q(f"UPDATE items SET {k}=:v WHERE id=:id", v=v, id=iid)
    if p.code is not None:
        enrich_form(it["form_id"])
    return get_form(it["form_id"], user)


@app.post("/api/forms/{fid}/refresh")
async def refresh(fid: int, user: dict = Depends(me)):
    notes = await run_in_threadpool(enrich_form, fid)
    f = get_form(fid, user)
    f["notes"] = notes
    return f


@app.post("/api/forms/{fid}/sent")
def mark_sent(fid: int, user: dict = Depends(need("office"))):
    db.q("UPDATE forms SET sent_at=now(), sent_by=:u WHERE id=:id", u=user["user"], id=fid)
    return get_form(fid, user)


@app.delete("/api/forms/{fid}")
def delete_form(fid: int, user: dict = Depends(need("office"))):
    db.q("DELETE FROM forms WHERE id=:id", id=fid)
    return {"ok": True}


# ---------- файлове ----------
def _fname(f: dict, suffix: str) -> str:
    base = "".join(ch for ch in (f.get("supplier") or "dostavchik") if ch.isalnum() or ch in " -_")[:40].strip()
    return f"{base} - {suffix}.xlsx"


def _dl(data: bytes, name: str, ctype: str) -> Response:
    from urllib.parse import quote
    return Response(data, media_type=ctype,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/api/forms/{fid}/answer.xlsx")
def answer(fid: int, user: dict = Depends(me)):
    f = db.one("SELECT * FROM forms WHERE id=:id", id=fid)
    items = db.rows("SELECT * FROM items WHERE form_id=:f ORDER BY pos", f=fid)
    return _dl(extras.answer_xlsx(f, items), _fname(f, "отговор"), XLSX)


@app.get("/api/forms/{fid}/blank.xlsx")
def blank(fid: int, user: dict = Depends(me)):
    f = db.one("SELECT * FROM forms WHERE id=:id", id=fid)
    items = [_item_calc(it, f["kind"]) for it in db.rows("SELECT * FROM items WHERE form_id=:f ORDER BY pos", f=fid)]
    return _dl(extras.blank_xlsx(f, items), _fname(f, "бланка 6Б"), XLSX)


@app.get("/api/forms/{fid}/filled.xlsx")
def filled(fid: int, user: dict = Depends(me)):
    f = db.one("SELECT * FROM forms WHERE id=:id", id=fid)
    items = db.rows("SELECT * FROM items WHERE form_id=:f ORDER BY pos", f=fid)
    data = extras.fill_original(f, items)
    if not data:
        raise HTTPException(404, "Оригиналът не е Excel с колона „Одобрен“ - свали „Отговор“.")
    return _dl(data, _fname(f, "попълнена"), XLSX)


@app.get("/api/forms/{fid}/original")
def original(fid: int, user: dict = Depends(me)):
    f = db.one("SELECT file_name, file_type, file_data FROM forms WHERE id=:id", id=fid)
    if not f or not f["file_data"]:
        raise HTTPException(404, "Няма файл.")
    return _dl(bytes(f["file_data"]), f["file_name"] or "blanka", f["file_type"] or "application/octet-stream")


# ---------- система ----------
@app.get("/health")
def health():
    return {"ok": True, "mistral": mistral.regions(), "mail": bool(os.getenv("IMAP_HOST")),
            "ai": bool(os.getenv("ANTHROPIC_API_KEY"))}


@app.get("/api/admin/probe")
async def probe(barcode: str, user: dict = Depends(need())):
    """Проверка на Мистрал по един баркод (само за админ)."""
    from . import minmax
    res, notes = await run_in_threadpool(mistral.enrich, [{"id": 0, "barcode": barcode, "code": None}])
    mm, n2 = await run_in_threadpool(minmax.info, [r["code"] for r in res if r.get("code")])
    return {"result": res, "minmax": mm, "notes": notes + n2}


@app.post("/api/mail/check")
async def mail_now(user: dict = Depends(need("office"))):
    return {"new": await run_in_threadpool(extras.poll_mail, ingest)}


INDEX = Path(__file__).with_name("index.html")


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX.read_text(encoding="utf-8")


async def _mail_loop():
    while True:
        try:
            await run_in_threadpool(extras.poll_mail, ingest)
        except Exception as e:  # noqa: BLE001
            print("mail:", e, flush=True)
        await asyncio.sleep(MAIL_EVERY * 60)


@app.on_event("startup")
async def _start():
    db.init()
    if os.getenv("IMAP_HOST"):
        asyncio.create_task(_mail_loop())
