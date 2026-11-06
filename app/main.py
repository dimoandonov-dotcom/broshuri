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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import db, extras, kzp, mistral, parse

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


def supkey(name: str | None) -> str:
    return " ".join((name or "").upper().replace("„", "").replace("“", "").replace('"', "").split())


def remember_reg(supplier: str | None, code: int | None, reg, source: str, user: str = "",
                 base=None, to_pct=None):
    """Запомня базовата цена, Т.О. и редовната доставна цена на доставчика за артикула -
    ползват се в следващата му брошура."""
    if not code or reg is None or not supkey(supplier):
        return
    db.q("""INSERT INTO supplier_prices (supplier, code, reg, base, to_pct, source, updated_by)
            VALUES (:s, :c, :r, :b, :t, :src, :u)
            ON CONFLICT (supplier, code) DO UPDATE SET reg=EXCLUDED.reg,
            base=COALESCE(EXCLUDED.base, supplier_prices.base), to_pct=COALESCE(EXCLUDED.to_pct, supplier_prices.to_pct),
            source=EXCLUDED.source, updated_by=EXCLUDED.updated_by, updated_at=now()""",
         s=supkey(supplier), c=int(code), r=reg, b=base, t=to_pct, src=source, u=user)


def enrich_form(form_id: int) -> list[str]:
    items = db.rows("SELECT id, barcode, code FROM items WHERE form_id=:f ORDER BY pos", f=form_id)
    fm = db.one("SELECT supplier, source, region_varna, region_sofia FROM forms WHERE id=:id", id=form_id) or {}
    rv, rs = fm.get("region_varna"), fm.get("region_sofia")
    if rv is None and rs is None:
        rv = rs = True
    office, sup_key = fm.get("source") == "office", supkey(fm.get("supplier"))
    res, notes = mistral.enrich(items)
    from . import minmax
    mm, mm_notes = minmax.info([r["code"] for r in res if r.get("code")])
    notes += mm_notes
    for r in res:
        x = mm.get(r.get("code") or -1, {})
        if x.get("mm_sales30") is not None:
            r["sales30_varna"], r["sales30_sofia"] = x["mm_varna"], x["mm_sofia"]
        # продажбите, по които се смята „доплащаме“ - само за регионите в брошурата
        if r.get("sales30_varna") is not None or r.get("sales30_sofia") is not None:
            r["sales30"] = (r.get("sales30_varna") or 0) * bool(rv) + (r.get("sales30_sofia") or 0) * bool(rs)
        db.q("""UPDATE items SET margin30=:a, stock=:b, stores=:c, last_promo=:d, uplift=:e WHERE id=:id""",
             id=r["id"], a=x.get("margin30"), b=x.get("stock"), c=x.get("mm_stores"),
             d=x.get("last_promo"), e=x.get("uplift"))
        db.q("""UPDATE items SET barcode=COALESCE(barcode, :bc), vat=COALESCE(:vat, vat, 20),
                m_delivery=:md, m_delivery_date=:mdd WHERE id=:id""", id=r["id"], bc=r.get("barcode"),
             vat=r.get("vat"), md=r.get("last_delivery"), mdd=r.get("last_delivery_date"))
        # бланка от офиса без редовна цена: първо запомнената за този доставчик, после последната от Мистрал
        if office and r.get("code"):
            row = db.one("SELECT reg FROM items WHERE id=:id", id=r["id"])
            if row and row["reg"] is None:
                mem = db.one("SELECT reg, base, to_pct FROM supplier_prices WHERE supplier=:s AND code=:c",
                             s=sup_key, c=r["code"])
                if mem:
                    db.q("""UPDATE items SET reg=:v, base=COALESCE(base, :b), to_pct=COALESCE(to_pct, :t),
                            reg_src='запомнена' WHERE id=:id""", v=mem["reg"], b=mem["base"], t=mem["to_pct"], id=r["id"])
                elif r.get("last_delivery"):
                    db.q("UPDATE items SET reg=:v, reg_src='Мистрал' WHERE id=:id", v=r["last_delivery"], id=r["id"])
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
    db.q("UPDATE items SET reg_src='доставчик' WHERE form_id=:f AND reg IS NOT NULL", f=fid)
    for it in db.rows("SELECT code, reg, base, to_pct FROM items WHERE form_id=:f AND code IS NOT NULL AND reg IS NOT NULL",
                      f=fid):
        remember_reg(p["supplier"], it["code"], it["reg"], "бланка от доставчика", user, it["base"], it["to_pct"])
    if notes:
        w = "\n".join(filter(None, ["\n".join(p["warnings"]), *notes]))
        db.q("UPDATE forms SET warnings=:w WHERE id=:id", w=w, id=fid)
    url = f"{extras.PUBLIC_URL}/#f{fid}" if extras.PUBLIC_URL else ""
    # Асен получава известие, когато офисът натисне „Изпрати на Асен“ (след проверка и поправки)
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


@app.get("/api/kzp")
def kzp_info(user: dict = Depends(me)):
    return kzp.meta() or {}


@app.post("/api/kzp/upload")
async def kzp_upload(file: UploadFile = File(...), user: dict = Depends(need("office"))):
    data = await file.read()
    try:
        n = await run_in_threadpool(kzp.load_xlsx, data, file.filename or "kzp.xlsx", user["user"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Файлът не става: {e}")
    return {"n": n}


@app.get("/api/kzp/report")
async def kzp_report(start: str | None = None, user: dict = Depends(me)):
    """Справка КЗП за брошура с начало `start`: кои артикули могат да влязат и кои не (активация 30/15 дни)."""
    from datetime import date as _date
    d0 = _date.fromisoformat(start) if start else _date.today()
    rows = db.rows("SELECT * FROM kzp ORDER BY grp, category, name")

    def run():
        from . import minmax
        acts = kzp.activations({r["code"]: (kzp.DAYS_SHORT if r["perishable"] else kzp.DAYS_LONG) for r in rows}, d0)
        mm, _ = minmax.info([r["code"] for r in rows])
        return acts, mm
    acts, mm = await run_in_threadpool(run)
    out = []
    for r in rows:
        a = acts.get(r["code"], [])
        x = mm.get(r["code"], {})
        out.append({**r, "days": kzp.DAYS_SHORT if r["perishable"] else kzp.DAYS_LONG,
                    "blocked": bool(a), "activations": [{**v, "start": v["start"].isoformat(), "end": v["end"].isoformat()} for v in a],
                    "free_from": (max(v["end"] for v in a) + timedelta(days=(kzp.DAYS_SHORT if r["perishable"]
                                                                               else kzp.DAYS_LONG) + 1)).isoformat() if a else None,
                    "sales30": x.get("mm_sales30"), "margin30": x.get("margin30"), "stock": x.get("stock")})
    return {"start": d0.isoformat(), "meta": kzp.meta(), "items": out}


@app.get("/api/kzp/codes")
def kzp_codes(user: dict = Depends(me)):
    return [r["code"] for r in db.rows("SELECT code FROM kzp")]


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
    notify: bool = True   # само админ може да го спре (за тестове)


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
    if p.notify or user["role"] != "admin":
        pass  # известието към Асен тръгва с бутона „Изпрати на Асен“
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
    if sup_gives is None and kind != "comp" and reg is not None:
        sup_gives = 0.0   # доставчикът още не е дал отстъпка - цялото намаление е наше
    it["cost"] = cost
    it["margin"] = round((app_ - cost) / app_ * 100, 1) if app_ and cost is not None else None
    # ако доставчикът още не е дал промо цена - маржът при сегашната редовна доставна (най-лошият случай)
    it["margin_at_reg"] = round((app_ - reg) / app_ * 100, 1) if app_ and reg is not None else None
    it["margin_best"] = it["margin"] if it["margin"] is not None else it["margin_at_reg"]
    it["reg_margin"] = round((shelf - reg) / shelf * 100, 1) if shelf and reg is not None else None
    it["off_pct"] = round((1 - app_ / shelf) * 100) if app_ and shelf else None
    it["funded"] = round((shelf - app_) - sup_gives, 4) if app_ and shelf and sup_gives is not None else None
    # каква промо доставна цена ни трябва, за да имаме MIN_MARGIN при тази рафтова цена
    it["need_delivery"] = round(app_ * (1 - MIN_MARGIN / 100), 4) if app_ else None
    it["need_dto"] = round((1 - it["need_delivery"] / reg) * 100, 1) if app_ and reg else None
    it["min_margin"] = MIN_MARGIN
    k = it.get("kzp")
    if k:
        # КЗП: маржът в промоцията не може да надминава регулярния марж
        it["kzp_max_price"] = (int(cost / (1 - it["reg_margin"] / 100) * 100 + 1e-6) / 100
                               if cost is not None and it["reg_margin"] is not None and it["reg_margin"] < 100 else None)
        it["kzp_ok_margin"] = (it["margin"] is None or it["reg_margin"] is None
                               or it["margin"] <= it["reg_margin"] + 0.05)
    it["checks"] = checks(it, kind)
    return it


def checks(it: dict, kind: str) -> list[str]:
    """Проверки за грешки на персонала - показват се в червено на реда."""
    out = []
    n = lambda k: float(it[k]) if it.get(k) is not None else None  # noqa: E731
    base, reg, promo, dto = n("base"), n("reg"), n("promo"), n("dto_pct")
    md, vat, app_, shelf, cost = n("m_delivery"), n("vat") or 20, n("approved"), n("shelf"), it.get("cost")
    k = it.get("kzp")
    if k:
        if k["activations"]:
            a = k["activations"][0]
            out.append(f"КЗП: артикулът е бил на активация {a['start'][8:10]}.{a['start'][5:7]}–{a['end'][8:10]}.{a['end'][5:7]}"
                       f"{(' (-' + format(a['discount'], '.0f') + '%)') if a.get('discount') else ''} в последните "
                       f"{k['days']} дни преди брошурата - НЕ може да влезе в брошурата.")
        if not it.get("kzp_ok_margin"):
            out.append(f"КЗП: маржът в промоцията {it['margin']:.1f}% е по-голям от регулярния {it['reg_margin']:.1f}%. "
                       f"Промо цената трябва да е най-много {it['kzp_max_price']:.2f} €.")
    if not it.get("code"):
        out.append("Няма наш код - артикулът не е намерен в Мистрал по баркода. "
                   "Потърси го по име в полето „Намери в Мистрал“ отдолу или напиши кода в „Наш код“.")
    if reg and md:
        net = md / (1 + vat / 100)
        if abs(reg - net) / net < 0.06 and vat:
            out.append(f"Редовната цена {reg:.4f} изглежда БЕЗ ДДС (последна доставка без ДДС {net:.4f}). "
                       f"С ДДС {vat:.0f}% е {reg * (1 + vat / 100):.4f}.")
        elif abs(reg - md) / md > 0.10:
            out.append(f"Редовната цена се различава с {(reg / md - 1) * 100:+.0f}% от последната доставка "
                       f"({md:.4f} € с ДДС{', ' + str(it.get('m_delivery_date')) if it.get('m_delivery_date') else ''}).")
    if base is not None and reg is not None and base + 1e-6 < reg:
        out.append("Базовата цена е по-ниска от редовната доставна.")
    if kind != "comp" and promo is not None and reg is not None and promo > reg + 1e-6:
        out.append("Промо доставната цена е по-висока от редовната.")
    if dto is not None and dto > 40:
        out.append(f"Д.Т.О. {dto:.1f}% е необичайно голяма - проверете дали не е въведена цена вместо процент.")
    if n("to_pct") is not None and n("to_pct") > 40:
        out.append("Т.О. над 40% - проверете.")
    if app_ is not None and cost is not None and app_ <= cost:
        out.append("Промо цената на рафта е под доставната - губим от всяка бройка.")
    if app_ is not None and shelf is not None and app_ > shelf + 1e-6:
        out.append("Промо цената е по-висока от сегашната цена на рафта.")
    return out


SUMMARY = """SELECT f.id, f.supplier, f.kind, f.brochure_no, f.promo_from, f.promo_to, f.status, f.source,
    f.created_at, f.decided_by, f.decided_at, f.sent_at, f.ready_at, f.returned_at,
    CASE WHEN f.sent_at IS NOT NULL THEN 'sent'
         WHEN f.returned_at IS NOT NULL AND (f.ready_at IS NULL OR f.returned_at >= f.ready_at) THEN 'back'
         WHEN f.ready_at IS NOT NULL THEN 'asen' ELSE 'prep' END AS stage, f.warnings IS NOT NULL AND f.warnings <> '' AS has_warn,
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
    items = db.rows("SELECT * FROM items WHERE form_id=:f ORDER BY pos", f=fid)
    kz = kzp.lookup([it["code"] for it in items])
    acts = {}
    if kz:
        from datetime import date as _date
        start = f.get("promo_from") or _date.today()
        try:
            acts = kzp.activations({c: (kzp.DAYS_SHORT if k["perishable"] else kzp.DAYS_LONG) for c, k in kz.items()}, start)
        except Exception as e:  # noqa: BLE001
            f["warnings"] = ((f.get("warnings") or "") + f"\nКЗП: не успях да проверя активациите ({e})").strip()
    for it in items:
        k = kz.get(it["code"])
        it["kzp"] = ({"category": k["category"], "perishable": k["perishable"],
                      "days": kzp.DAYS_SHORT if k["perishable"] else kzp.DAYS_LONG,
                      "activations": [{**a, "start": a["start"].isoformat(), "end": a["end"].isoformat()}
                                      for a in acts.get(it["code"], [])]} if k else None)
    f["items"] = [_item_calc(it, f["kind"]) for it in items]
    f["kzp_count"] = sum(1 for it in items if it["kzp"])
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
    reg: float | None = None
    base: float | None = None
    to_pct: float | None = None
    dto_pct: float | None = None
    promo: float | None = None
    clear: list[str] = []      # изтрити полета на доставчика (празно поле)
    net: bool = False          # цените са въведени БЕЗ ДДС - системата добавя ДДС по ставката на артикула
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
    # цени от доставчика, въведени от офиса: базова -> (Т.О.) -> редовна -> (Д.Т.О.) -> промо доставна
    prices = {k: getattr(p, k) for k in ("base", "to_pct", "reg", "dto_pct", "promo") if getattr(p, k) is not None}
    cl = [k for k in p.clear if k in ("base", "to_pct", "reg", "dto_pct", "promo")]
    if cl:
        if "dto_pct" in cl or "promo" in cl:
            cl = list(set(cl) | {"dto_pct", "promo"})
        db.q(f"UPDATE items SET {', '.join(k + '=NULL' for k in cl)} WHERE id=:id", id=iid)
    if prices and p.net:
        vat = float((db.one("SELECT vat FROM items WHERE id=:id", id=iid) or {}).get("vat") or 20)
        for k in ("base", "reg", "promo"):
            if k in prices:
                prices[k] = round(prices[k] * (1 + vat / 100), 4)
    if prices:
        cur = db.one("SELECT base, to_pct, reg, dto_pct, promo FROM items WHERE id=:id", id=iid)
        v = {k: (float(x) if x is not None else None) for k, x in cur.items()}
        v.update(prices)
        if ("base" in prices or "to_pct" in prices) and v["base"] is not None and "reg" not in prices:
            v["reg"] = round(v["base"] * (1 - (v["to_pct"] or 0) / 100), 4)
        if "promo" in prices and v["reg"]:
            v["dto_pct"] = round((1 - v["promo"] / v["reg"]) * 100, 2)
        elif v["dto_pct"] is not None and v["reg"] is not None:
            v["promo"] = round(v["reg"] * (1 - v["dto_pct"] / 100), 4)
        db.q("""UPDATE items SET base=:base, to_pct=:to_pct, reg=:reg, dto_pct=:dto_pct, promo=:promo,
                reg_src='офис', dto_src=CASE WHEN :dc THEN 'офис' ELSE dto_src END WHERE id=:id""",
             id=iid, dc=("dto_pct" in prices or "promo" in prices), **v)
        row = db.one("""SELECT f.supplier, i.code FROM items i JOIN forms f ON f.id=i.form_id WHERE i.id=:id""", id=iid)
        remember_reg(row["supplier"], row["code"], v["reg"], "поправена", user["user"], v["base"], v["to_pct"])
    if p.code is not None:
        enrich_form(it["form_id"])
        if not db.one("SELECT 1 AS x FROM items WHERE form_id=:f AND code IS NULL", f=it["form_id"]):
            w = db.one("SELECT warnings FROM forms WHERE id=:f", f=it["form_id"])["warnings"] or ""
            w = "\n".join(x for x in w.split("\n") if "без код" not in x and "Без пълен баркод" not in x)
            db.q("UPDATE forms SET warnings=:w WHERE id=:f", w=w, f=it["form_id"])
    return get_form(it["form_id"], user)


@app.post("/api/forms/{fid}/refresh")
async def refresh(fid: int, user: dict = Depends(me)):
    notes = await run_in_threadpool(enrich_form, fid)
    w = (db.one("SELECT warnings FROM forms WHERE id=:f", f=fid) or {}).get("warnings") or ""
    w = "\n".join(x for x in w.split("\n") if not x.startswith(("MinMaxAI:", "Мистрал ")))
    db.q("UPDATE forms SET warnings=:w WHERE id=:f", w="\n".join(filter(None, [w, *notes])), f=fid)
    f = get_form(fid, user)
    f["notes"] = notes
    return f


@app.post("/api/forms/{fid}/ready")
def mark_ready(fid: int, user: dict = Depends(need("office"))):
    """Офисът е проверил бланката - отива при Асен (Telegram с линк)."""
    f = get_form(fid, user)
    db.q("UPDATE forms SET ready_at=now(), ready_by=:u WHERE id=:id", u=user["user"], id=fid)
    n_chk = sum(len(it["checks"]) for it in f["items"])
    kz = f.get("kzp_count") or 0
    url = f"{extras.PUBLIC_URL}/#f{fid}" if extras.PUBLIC_URL else ""
    extras.telegram(f"Брошура за цени: {f['supplier'] or 'доставчик'} - {len(f['items'])} арт."
                    + (f", {kz} от КЗП" if kz else "") + (f", {n_chk} за проверка" if n_chk else "")
                    + f"\nПодготви: {user['user']}\n{url}")
    return get_form(fid, user)


@app.post("/api/forms/{fid}/return")
def return_to_office(fid: int, user: dict = Depends(need("approver"))):
    """Асен натиска „Запис“: цените му са записани, празните Д.Т.О. се попълват автоматично
    (за 20% марж, при КЗП - не над регулярния марж) и бланката се връща при офиса за изпращане."""
    f = get_form(fid, user)
    for it in f["items"]:
        if it.get("dto_pct") is not None or it.get("approved") is None or not it.get("reg") or f["kind"] == "comp":
            continue
        reg, app_ = float(it["reg"]), float(it["approved"])
        target = MIN_MARGIN
        if it.get("kzp") and it.get("reg_margin") is not None:
            target = min(target, float(it["reg_margin"]))
        need = app_ * (1 - target / 100)
        dto = max(0.0, round((1 - need / reg) * 100, 1))
        db.q("UPDATE items SET dto_pct=:d, promo=:p, dto_src='авто' WHERE id=:id",
             d=dto, p=round(reg * (1 - dto / 100), 4), id=it["id"])
    st = f["status"] if f["status"] != "pending" else "approved"
    db.q("""UPDATE forms SET status=:s, decided_by=COALESCE(decided_by, :u), decided_at=COALESCE(decided_at, now()),
            returned_at=now(), returned_by=:u WHERE id=:id""", s=st, u=user["user"], id=fid)
    return get_form(fid, user)


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


@app.get("/api/admin/columns")
async def columns(table: str, code: int | None = None, user: dict = Depends(need())):
    """Колоните на таблица в Мистрал (+ един ред за артикул) - за проучване, само админ."""
    def run():
        with mistral.connect("varna") as conn:
            cur = conn.cursor()
            cols = mistral._cols(cur, table)
            row = None
            if code and "MATERIALCODE" in [c.upper() for c in cols]:
                cur.execute(f"SELECT TOP 1 * FROM [{table}] WITH (NOLOCK) WHERE MATERIALCODE = %s", (code,))
                row = {k: str(v) for k, v in (cur.fetchone() or {}).items()}
            return {"columns": cols, "row": row}
    return await run_in_threadpool(run)


@app.post("/api/mail/check")
async def mail_now(user: dict = Depends(need("office"))):
    return {"new": await run_in_threadpool(extras.poll_mail, ingest)}


INDEX = Path(__file__).with_name("index.html")
LOGO = Path(__file__).with_name("static") / "logo.png"


@app.get("/logo.png")
def logo():
    from fastapi.responses import FileResponse
    return FileResponse(LOGO, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


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
    kzp.init()
    if os.getenv("IMAP_HOST"):
        asyncio.create_task(_mail_loop())
