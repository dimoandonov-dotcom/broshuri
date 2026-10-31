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
