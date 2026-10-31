"""Имейл кутия (IMAP), Telegram известия и Excel отговор към доставчика."""
from __future__ import annotations

import email
import imaplib
import io
import json
import os
import urllib.request
from email.header import decode_header, make_header

from . import db

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
ST = {"pending": "Чака решение", "approved": "Одобрена", "counter": "Контраоферта", "rejected": "Отказана"}


# ---------- Telegram ----------
def _chat_ids() -> list[str]:
    """TELEGRAM_CHAT_IDS изрично, или по потребител (TELEGRAM_USERS) от свързаните в бота на MinMaxAI."""
    ids = [c.strip() for c in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
    url, names = os.getenv("MINMAX_DATABASE_URL", ""), os.getenv("TELEGRAM_USERS", "asen")
    if not ids and url:
        try:
            from sqlalchemy import create_engine, text
            url = url.replace("postgres://", "postgresql+psycopg2://", 1).replace("postgresql://", "postgresql+psycopg2://", 1)
            eng = create_engine(url, pool_pre_ping=True)
            with eng.connect() as c:
                want = [n.strip().lower() for n in names.split(",") if n.strip()]
                ids = [str(r[0]) for r in c.execute(text("SELECT chat_id FROM tg_users WHERE lower(username) = ANY(:u)"),
                                                    {"u": want})]
            eng.dispose()
        except Exception as e:  # noqa: BLE001
            print("telegram ids:", e, flush=True)
    return ids


def telegram(text_: str) -> None:
    tok = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not tok:
        return
    for chat in _chat_ids():
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                data=json.dumps({"chat_id": chat, "text": text_, "disable_web_page_preview": True}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:  # noqa: BLE001
            pass


# ---------- имейл ----------
def _dec(s) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:  # noqa: BLE001
        return s or ""


def poll_mail(ingest) -> int:
    """Чете непрочетените писма в IMAP кутията; всеки прикачен Excel/PDF/снимка става бланка."""
    host, user, pw = os.getenv("IMAP_HOST"), os.getenv("IMAP_USER"), os.getenv("IMAP_PASSWORD")
    if not (host and user and pw):
        return 0
    from .parse import file_kind
    n = 0
    m = imaplib.IMAP4_SSL(host, int(os.getenv("IMAP_PORT", "993")))
    try:
        m.login(user, pw)
        m.select(os.getenv("IMAP_FOLDER", "INBOX"))
        _, data = m.search(None, "UNSEEN")
        for num in (data[0] or b"").split()[:30]:
            _, msg_data = m.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            mid = (msg.get("Message-ID") or f"{user}:{num.decode()}").strip()
            if db.one("SELECT 1 AS x FROM mail_seen WHERE msg_id=:m", m=mid):
                continue
            frm = _dec(msg.get("From"))
            for part in msg.walk():
                fn = _dec(part.get_filename())
                if not fn or not file_kind(fn, part.get_content_type()):
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    ingest(payload, fn, part.get_content_type(), source="email", source_from=frm, user="имейл")
                    n += 1
            db.q("INSERT INTO mail_seen(msg_id) VALUES (:m) ON CONFLICT DO NOTHING", m=mid)
    finally:
        try:
            m.logout()
        except Exception:  # noqa: BLE001
            pass
    return n


# ---------- Excel отговор ----------
def _off(cur, app):
    return round((1 - app / cur) * 100) if cur and app else None


def answer_xlsx(form: dict, items: list[dict]) -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Отговор"
    red = Font(bold=True, color="C0193F")
    ws.append([f"Отговор по заявка {('№ ' + form['brochure_no']) if form.get('brochure_no') else ''} - {form['supplier'] or 'доставчик'}"])
    ws["A1"].font = Font(bold=True, size=14)
    per = f"{form['promo_from'] or ''} - {form['promo_to'] or ''}"
    ws.append([f"Период на промоцията: {per}", "", f"Решение: {ST.get(form['status'], form['status'])}",
               "", f"Формат: {form.get('fmt') or form.get('wanted_format') or ''}"])
    if form.get("note"):
        ws.append([f"Бележка: {form['note']}"])
        ws.cell(ws.max_row, 1).font = red
    ws.append([])
    head = ["№", "Артикул", "Баркод", "Наш код", "Базова цена с ДДС", "Т.О. %", "Регулярна доставна",
            "Д.Т.О. %", "Промо доставна" if form["kind"] != "comp" else "Компенсация/бр.",
            "Цена рафт сега", "Одобрена промо цена", "Отстъпка %", "Бележка"]
    ws.append(head)
    hr = ws.max_row
    for c in ws[hr]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E3E8EE")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for i, it in enumerate(items, 1):
        f = lambda v: float(v) if v is not None else None  # noqa: E731
        ws.append([i, it["name"], it.get("barcode"), it.get("code"), f(it.get("base")), f(it.get("to_pct")),
                   f(it.get("reg")), f(it.get("dto_pct")),
                   f(it.get("comp")) if form["kind"] == "comp" else f(it.get("promo")),
                   f(it.get("shelf")), f(it.get("approved")), _off(f(it.get("shelf")), f(it.get("approved"))),
                   it.get("item_note")])
        ws.cell(ws.max_row, 11).font = red
    for col, w in zip("ABCDEFGHIJKLM", (4, 40, 16, 9, 11, 8, 11, 8, 11, 10, 12, 10, 30)):
        ws.column_dimensions[col].width = w
    ws.row_dimensions[hr].height = 32
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def fill_original(form: dict, items: list[dict]) -> bytes | None:
    """Попълва колоната „Одобрен“ в оригиналния Excel на доставчика (ако е .xlsx и я намира)."""
    if not (form.get("file_data") and (form.get("file_name") or "").lower().endswith((".xlsx", ".xlsm"))):
        return None
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(bytes(form["file_data"])))
    by_bc = {it["barcode"]: it for it in items if it.get("barcode")}
    by_name = {(it["name"] or "").strip().lower(): it for it in items}
    done = 0
    for ws in wb.worksheets:
        appr = bc_col = name_col = None
        head_row = None
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 40)):
            for c in row:
                v = str(c.value or "").strip().lower()
                if v.startswith("одобрен") and appr is None:
                    appr, head_row = c.column, c.row
                elif v == "баркод":
                    bc_col = c.column
                elif v == "артикул":
                    name_col = c.column
        if not appr:
            continue
        for r in range(head_row + 1, ws.max_row + 1):
            bc = ws.cell(r, bc_col).value if bc_col else None
            if isinstance(bc, float):
                bc = str(int(bc))
            it = by_bc.get(str(bc or "").strip()) or by_name.get(str(ws.cell(r, name_col).value or "").strip().lower() if name_col else "")
            if not it or it.get("approved") is None:
                continue
            app, cur = float(it["approved"]), float(it["shelf"]) if it.get("shelf") else None
            off = _off(cur, app)
            ws.cell(r, appr).value = f"{app:.2f} €" + (f" (-{off}%)" if off else "")
            done += 1
    if not done:
        return None
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def blank_xlsx(form: dict, items: list[dict]) -> bytes:
    """Бланката на веригата (Приложение 6Б, при компенсация 6Г) - същият вид и текстове като хартиената.
    Заглавията са като в шаблона, затова ако доставчикът я върне попълнена, системата я чете обратно."""
    import openpyxl
    from datetime import date
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.worksheet.page import PageMargins
    comp = form.get("kind") == "comp"
    office = form.get("source") == "office"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Заявка"
    F = lambda **k: Font(name="Arial", **k)  # noqa: E731
    fill = lambda c: PatternFill("solid", fgColor=c)  # noqa: E731
    DARK, GREY, YEL, BLUE = fill("595959"), fill("D9D9D9"), fill("FFF2CC"), fill("DDEBF7")
    thin, med = Side(style="thin", color="7F7F7F"), Side(style="medium", color="404040")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    C = Alignment(horizontal="center", vertical="center", wrap_text=True)
    L = Alignment(horizontal="left", vertical="center", wrap_text=True)
    R = Alignment(horizontal="right", vertical="center")
    for col, w in zip("ABCDEFGHIJK", (5, 14, 38, 17, 12, 9, 13, 9, 13, 16, 18)):
        ws.column_dimensions[col].width = w

    def put(ref, val=None, font=None, al=None, fl=None, merge=None, border=None):
        c = ws[ref]
        c.value = val
        if font: c.font = font
        if al: c.alignment = al
        if fl: c.fill = fl
        if border: c.border = border
        if merge:
            ws.merge_cells(f"{ref}:{merge}")
        return c

    def fmt_d(d):
        return d.strftime("%d.%m.%Y") if d else ""

    no = form.get("brochure_no") or "___"
    put("A1", f"Приложение № {'6Г' if comp else '6Б'} към Договор за търговско сътрудничество",
        F(size=8, color="FFFFFF"), L, DARK, "K1")
    title = (f"Заявка Брошура №{no} за КОМПЕНСАЦИЯ ЗА ПРОДАДЕНА БРОЙКА от Доставчик:" if comp
             else f"Заявка № {no} за участие в РЕКЛАМНА БРОШУРА от Доставчик:")
    put("A3", title, F(size=15, bold=True), C, None, "H3")
    put("I3", form.get("supplier") or "", F(size=13, bold=True), C, None, "K3")
    ws.row_dimensions[3].height = 30
    put("A4", ("Жълтите полета се попълват от ДОСТАВЧИКА, сините от КУПУВАЧА при потвърждение и при отчитане, а белите "
               "се изчисляват автоматично: регулярна доставна цена = базова с ДДС - Т.О.%") if comp else
        ("Жълтите полета се попълват от ДОСТАВЧИКА, сините от КУПУВАЧА при потвърждение, а белите се изчисляват "
         "автоматично: регулярна доставна цена = базова с ДДС - Т.О.%; промоционална доставна цена = регулярна - Д.Т.О.%"),
        F(size=8, italic=True), C, None, "K4")
    ws.row_dimensions[4].height = 24

    def period(r, label, a, b, note=None):
        put(f"A{r}", label, F(size=10, bold=True), L, None, f"C{r}")
        put(f"D{r}", "от", F(size=10), C, None, None, box)
        put(f"E{r}", fmt_d(a), F(size=10), C, YEL, None, box)
        put(f"F{r}", "до", F(size=10), C, None, None, box)
        put(f"G{r}", fmt_d(b), F(size=10), C, YEL, None, box)
        if note:
            put(f"H{r}", note, F(size=8, italic=True, color="595959"), L, None, f"J{r}")

    period(6, "Брошура — период на промоцията:", form.get("promo_from"), form.get("promo_to"),
           "(стандартна продължителност 15 дни)")
    period(7, "Период на засичане на компенсации:" if comp else "Период на промоционалните доставни цени:",
           form.get("price_from"), form.get("price_to"))
    yn = lambda v: "Да" if v else ("Не" if v is False else "")  # noqa: E731
    put("A9", "АКТИВАЦИЯ ПО РЕГИОНИ:", F(size=10, bold=True), L, GREY, "B9", box)
    put("C9", "Да / Не", F(size=10, bold=True), C, GREY, None, box)
    put("A10", "Активация — Варна", F(size=9), L, None, "B10", box)
    put("C10", yn(form.get("region_varna")), F(size=10), C, YEL, None, box)
    put("A11", "Активация — София", F(size=9), L, None, "B11", box)
    put("C11", yn(form.get("region_sofia")), F(size=10), C, YEL, None, box)
    put("E9", "Краен срок за подаване на заявката:", F(size=10, bold=True), R, None, "H9", box)
    put("I9", fmt_d(form.get("deadline")), F(size=10, bold=True), C, None, None, box)
    put("F10", "Снимков материал с високо качество се изпраща заедно със заявката",
        F(size=8, italic=True, color="595959"), C, None, "I10")
    if comp:
        put("A13", "ДОСТАВЧИКЪТ заявява компенсация в определен размер за всяка продадена бройка от посочените артикули "
                   "за посочения период. КУПУВАЧЪТ потвърждава заявката ПРЕДИ началото на периода.",
            F(size=8, italic=True), C, None, "K13")
    else:
        put("A13", "ПРАВИЛО ЗА ПЕРИОДИТЕ. Промоционалните доставни цени се залагат най-малко ЕДНА СЕДМИЦА "
                   "(7 календарни дни) ПРЕДИ началото на брошурата и остават в сила ПРЕЗ ЦЕЛИЯ период на брошурата.",
            F(size=8, italic=True), C, None, "K13")
        put("A14", "Цената за участие е съгласно Приложение № 4 – Маркетингов ценоразпис.",
            F(size=8, italic=True), C, None, "K14")
    ws.row_dimensions[13].height = 22

    hr = 17
    put(f"A{hr-1}", "Попълва се от ДОСТАВЧИКА", F(size=10, bold=True, color="FFFFFF"), C, DARK, f"J{hr-1}")
    put(f"K{hr-1}", "Попълва се от КУПУВАЧА", F(size=10, bold=True, color="FFFFFF"), C, DARK)
    head = (["№", "SAF-T", "Артикул", "Баркод", "Базова цена с ДДС (€)", "Т.О. %",
             "Регулярна доставна цена с ДДС (€)", "Компенсация за 1 брой с ДДС (€)", "Предложен период от",
             "Предложен период до", "Одобрен"] if comp else
            ["№", "SAF-T", "Артикул", "Баркод", "Базова цена с ДДС (€)", "Т.О. %",
             "Регулярна доставна цена с ДДС (€)", "Д.Т.О. %", "Промоционална доставна цена с ДДС (€)",
             "Желан формат на карето", "Одобрен"])
    for j, h in enumerate(head, 1):
        c = ws.cell(hr, j, h)
        c.font, c.alignment, c.fill, c.border = F(size=9, bold=True), C, GREY, box
    ws.row_dimensions[hr].height = 42

    n = lambda v: float(v) if v is not None else None  # noqa: E731
    asked = False
    rows = max(14, len(items))
    for i in range(rows):
        r = hr + 1 + i
        it = items[i] if i < len(items) else None
        vals = [i + 1] + [None] * 10
        if it:
            app_, shelf = n(it.get("approved")), n(it.get("shelf"))
            off = round((1 - app_ / shelf) * 100) if app_ and shelf else None
            appr = (f"{app_:.2f} €".replace(".", ",") + (f"  (-{off}%)" if off else "")) if app_ else None
            dto, promo = n(it.get("dto_pct")), n(it.get("promo"))
            req = office and dto is None and it.get("need_delivery") is not None
            if req:
                dto, promo, asked = it.get("need_dto"), it.get("need_delivery"), True
            vals = [i + 1, it.get("saft"), it.get("m_name") or it["name"], it.get("barcode"), n(it.get("base")),
                    n(it.get("to_pct")) / 100 if it.get("to_pct") is not None else None, n(it.get("reg"))]
            if comp:
                vals += [n(it.get("comp")), fmt_d(form.get("price_from")), fmt_d(form.get("price_to")), appr]
            else:
                vals += [dto / 100 if dto is not None else None, promo,
                         form.get("fmt") or form.get("wanted_format"), appr]
        for j, v in enumerate(vals, 1):
            c = ws.cell(r, j, v)
            c.border, c.font = box, F(size=9)
            c.alignment = L if j == 3 else C
            if j in (2, 4, 5, 6, 10) or (j == 8 and not comp):
                c.fill = YEL
            if j == 11:
                c.fill, c.font = BLUE, F(size=10, bold=True, color="C0193F")
        ws.cell(r, 4).number_format = "@"
        for j in (5, 7, 9) if not comp else (5, 7, 8):
            ws.cell(r, j).number_format = "0.0000"
        for j in (6, 8) if not comp else (6,):
            ws.cell(r, j).number_format = "0.00%"
        if it and not comp and office and it.get("dto_pct") is None and it.get("need_delivery") is not None:
            for j in (8, 9):
                ws.cell(r, j).font = F(size=9, bold=True, italic=True, color="C0193F")
        ws.row_dimensions[r].height = 18
    end = hr + rows
    for j in range(1, 12):
        c = ws.cell(end, j)
        c.border = Border(left=c.border.left, right=c.border.right, top=c.border.top, bottom=med)

    r = end + 1
    if asked:
        put(f"A{r}", f"В червено: искана от КУПУВАЧА промоционална доставна цена и Д.Т.О.% (минимален марж "
                     f"{float(items[0].get('min_margin') or 20):.0f}% при одобрената цена на рафта).",
            F(size=8, bold=True, italic=True, color="C0193F"), L, None, f"K{r}")
    if form.get("note"):
        r += 1
        put(f"A{r}", f"Бележка от КУПУВАЧА: {form['note']}", F(size=9, bold=True, color="C0193F"), L, None, f"K{r}")
    r += 1
    put(f"A{r}", "ДОСТАВЧИКЪТ ще получи потвърждение по e-mail за одобрените артикули и формата.",
        F(size=8, italic=True), C, None, f"K{r}")
    r += 2
    put(f"A{r}", "ДОСТАВЧИКА избира метода на документирането:", F(size=10, bold=True), C, None, f"K{r}")
    for t in ("ДОСТАВЧИКЪТ издава кредитно известие", "КУПУВАЧЪТ издава фактура"):
        r += 1
        put(f"A{r}", "☐", F(size=11), C, None, None, box)
        put(f"B{r}", t, F(size=9), L, None, f"F{r}", box)
    r += 2
    put(f"A{r}", "ДОСТАВЧИКА избира начин на уреждане:", F(size=10, bold=True), C, None, f"K{r}")
    for t in ("прихващане от задълженията на КУПУВАЧА към ДОСТАВЧИКА",
              "директен банков превод в срок до 30 календарни дни"):
        r += 1
        put(f"A{r}", "☐", F(size=11), C, None, None, box)
        put(f"B{r}", t, F(size=9), L, None, f"F{r}", box)
    r += 2
    put(f"A{r}", "ДАТА:", F(size=10, bold=True), L, None, f"B{r}", box)
    put(f"C{r}", date.today().strftime("%d.%m.%Y"), F(size=10), L, None, None, box)
    r += 1
    put(f"A{r}", "Настоящата заявка е неразделна част от Договора за търговско сътрудничество между страните и се "
                 "подчинява на раздел V, т. 2 и раздел VI от него. Попълнените и подписани от двете страни раздели "
                 "съставляват предварително договорените ясни и недвусмислени условия по смисъла на чл. 37е, ал. 1 от ЗЗК.",
        F(size=7, italic=True, color="595959"), L, None, f"K{r}")
    ws.row_dimensions[r].height = 26
    r += 2
    put(f"A{r}", "ЗА ДОСТАВЧИКА:", F(size=10, bold=True), Alignment(horizontal="left", vertical="center"), None, f"B{r}")
    put(f"C{r}", "", F(size=10), L)
    ws[f"C{r}"].border = Border(bottom=thin)
    put(f"E{r}", "ДАТА:", F(size=10, bold=True), R)
    put(f"F{r}", "", None, None, None, f"G{r}", box)
    put(f"H{r}", "ЗА КУПУВАЧА:", F(size=10, bold=True), R, None, f"I{r}")
    put(f"J{r}", os.getenv("BUYER_NAME", "Габриела Петрова"), F(size=10, bold=True), L, None, f"K{r}")
    r += 1
    put(f"C{r}", "(подпис и име)", F(size=7, italic=True, color="7F7F7F"), C)
    put(f"J{r}", "(подпис и име)", F(size=7, italic=True, color="7F7F7F"), C, None, f"K{r}")
    put(f"A{r+2}", f"Решение: {ST.get(form['status'], form['status'])}", F(size=8, color="7F7F7F"), L, None, f"E{r+2}")

    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins = PageMargins(left=0.3, right=0.3, top=0.4, bottom=0.4)
    ws.print_area = f"A1:K{r+2}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
