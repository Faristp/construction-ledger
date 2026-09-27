"""Construction finance ledger: shareholders + their payments, FastAPI + Postgres."""
import csv
import io
import os
import secrets
from datetime import date, datetime
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from db import db, init_db

BASE = Path(__file__).parent
SESSION_COOKIE = "ledger_session"

app = FastAPI(title="Construction Ledger")
init_db()


# ---------- admin auth ----------
# Everyone can VIEW the ledger. Adding, editing, deleting, and importing
# requires this password, set once as the ADMIN_PASSWORD environment
# variable on the host (Render: Environment tab on the web service).
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError(
        "Set the ADMIN_PASSWORD environment variable before starting the app "
        "(this is the password you'll use to log in and make changes)."
    )


def require_admin(session: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    if not session:
        raise HTTPException(401, "Login required to make changes")
    with db() as con:
        row = con.execute("SELECT 1 FROM admin_sessions WHERE token=:t", {"t": session}).fetchone()
    if not row:
        raise HTTPException(401, "Login required to make changes")


class LoginIn(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(401, "Wrong password")
    token = secrets.token_urlsafe(32)
    with db() as con:
        con.execute("INSERT INTO admin_sessions(token) VALUES (:t)", {"t": token})
    # secure=True (cookie only sent over https) unless explicitly disabled for local http testing
    cookie_secure = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                         secure=cookie_secure, max_age=60 * 60 * 24 * 30)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    if session:
        with db() as con:
            con.execute("DELETE FROM admin_sessions WHERE token=:t", {"t": session})
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/session")
def session_status(session: str | None = Cookie(default=None, alias=SESSION_COOKIE)):
    if not session:
        return {"admin": False}
    with db() as con:
        row = con.execute("SELECT 1 FROM admin_sessions WHERE token=:t", {"t": session}).fetchone()
    return {"admin": bool(row)}


# ---------- models ----------
class ShareholderIn(BaseModel):
    name: str = Field(min_length=1)
    notes: str = ""


class EntryIn(BaseModel):
    shareholder_id: int
    entry_date: date
    purpose: str = Field(min_length=1)
    amount: float = Field(ge=0)  # rupees, converted to paise for storage


def to_paise(rupees: float) -> int:
    return round(rupees * 100)


def entry_row(r):
    return {
        "id": r["id"],
        "shareholder_id": r["shareholder_id"],
        "entry_date": r["entry_date"],
        "purpose": r["purpose"],
        "amount": r["amount"] / 100,
    }


# ---------- shareholders ----------
@app.get("/api/shareholders")
def list_shareholders():
    with db() as con:
        rows = con.execute(
            """SELECT s.id, s.name, s.notes,
                      COALESCE(SUM(e.amount), 0) AS total, COUNT(e.id) AS n
               FROM shareholders s LEFT JOIN entries e ON e.shareholder_id = s.id
               GROUP BY s.id ORDER BY s.name"""
        ).fetchall()
    grand = sum(r["total"] for r in rows)
    return {
        "grand_total": grand / 100,
        "shareholders": [
            {
                "id": r["id"],
                "name": r["name"],
                "notes": r["notes"],
                "total": r["total"] / 100,
                "entries": r["n"],
                "percent": round(r["total"] * 100 / grand, 2) if grand else 0,
            }
            for r in rows
        ],
    }


@app.post("/api/shareholders", status_code=201, dependencies=[Depends(require_admin)])
def add_shareholder(body: ShareholderIn):
    try:
        with db() as con:
            cur = con.execute(
                "INSERT INTO shareholders(name, notes) VALUES (:name, :notes) RETURNING id",
                {"name": body.name.strip(), "notes": body.notes},
            )
            return {"id": cur.fetchone()["id"], "name": body.name.strip()}
    except IntegrityError:
        raise HTTPException(409, "A shareholder with that name already exists")


@app.put("/api/shareholders/{sid}", dependencies=[Depends(require_admin)])
def rename_shareholder(sid: int, body: ShareholderIn):
    try:
        with db() as con:
            cur = con.execute(
                "UPDATE shareholders SET name=:name, notes=:notes WHERE id=:id",
                {"name": body.name.strip(), "notes": body.notes, "id": sid},
            )
            if not cur.rowcount:
                raise HTTPException(404, "Not found")
    except IntegrityError:
        raise HTTPException(409, "A shareholder with that name already exists")
    return {"ok": True}


@app.delete("/api/shareholders/{sid}", dependencies=[Depends(require_admin)])
def delete_shareholder(sid: int):
    with db() as con:
        cur = con.execute("DELETE FROM shareholders WHERE id=:id", {"id": sid})
        if not cur.rowcount:
            raise HTTPException(404, "Not found")
    return {"ok": True}


# ---------- entries ----------
@app.get("/api/entries")
def list_entries(shareholder_id: int | None = None, q: str = ""):
    sql = "SELECT * FROM entries WHERE 1=1"
    params: dict = {}
    if shareholder_id:
        sql += " AND shareholder_id = :shareholder_id"
        params["shareholder_id"] = shareholder_id
    if q:
        sql += " AND purpose ILIKE :q"
        params["q"] = f"%{q}%"
    sql += " ORDER BY entry_date, id"
    with db() as con:
        return [entry_row(r) for r in con.execute(sql, params).fetchall()]


@app.post("/api/entries", status_code=201, dependencies=[Depends(require_admin)])
def add_entry(body: EntryIn):
    with db() as con:
        _require_shareholder(con, body.shareholder_id)
        cur = con.execute(
            """INSERT INTO entries(shareholder_id, entry_date, purpose, amount)
               VALUES (:sid, :d, :purpose, :amount) RETURNING id""",
            {"sid": body.shareholder_id, "d": body.entry_date, "purpose": body.purpose.strip(),
             "amount": to_paise(body.amount)},
        )
        return {"id": cur.fetchone()["id"]}


@app.put("/api/entries/{eid}", dependencies=[Depends(require_admin)])
def update_entry(eid: int, body: EntryIn):
    with db() as con:
        _require_shareholder(con, body.shareholder_id)
        cur = con.execute(
            """UPDATE entries SET shareholder_id=:sid, entry_date=:d, purpose=:purpose, amount=:amount
               WHERE id=:id""",
            {"sid": body.shareholder_id, "d": body.entry_date, "purpose": body.purpose.strip(),
             "amount": to_paise(body.amount), "id": eid},
        )
        if not cur.rowcount:
            raise HTTPException(404, "Not found")
    return {"ok": True}


@app.delete("/api/entries/{eid}", dependencies=[Depends(require_admin)])
def delete_entry(eid: int):
    with db() as con:
        cur = con.execute("DELETE FROM entries WHERE id=:id", {"id": eid})
        if not cur.rowcount:
            raise HTTPException(404, "Not found")
    return {"ok": True}


def _require_shareholder(con, sid):
    if not con.execute("SELECT 1 FROM shareholders WHERE id=:id", {"id": sid}).fetchone():
        raise HTTPException(404, "Shareholder not found")


# ---------- CSV import / export ----------
DATE_FORMATS = ["%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y"]


def parse_date(text: str, default_year: int) -> date:
    text = text.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    # Sheet style with no year, e.g. "19-Feb" / "3-Mar"
    for fmt in ("%d-%b", "%d %b"):
        try:
            d = datetime.strptime(f"{text}-{default_year}", fmt + "-%Y")
            return d.date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date: {text!r}")


def parse_amount(text: str) -> float:
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    if not cleaned:
        raise ValueError(f"Unrecognised amount: {text!r}")
    return float(cleaned)


@app.post("/api/import", dependencies=[Depends(require_admin)])
async def import_csv(
    file: UploadFile = File(...),
    default_year: int = Form(...),
    shareholder_name: str = Form(""),
):
    """CSV columns: date, purpose, amount[, shareholder].
    If the file has no shareholder column, pass shareholder_name for the whole file."""
    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise HTTPException(400, "Empty file")
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    # tolerate headers like "purpose LATHEEF" and the sheet's "Column1"
    purpose_col = next((cols[c] for c in cols if c.startswith("purpose")), None)
    sh_col = cols.get("shareholder") or cols.get("column1") or cols.get("name")
    if "date" not in cols or not purpose_col or "amount" not in cols:
        raise HTTPException(400, "CSV needs columns: date, purpose, amount")
    if not sh_col and not shareholder_name.strip():
        raise HTTPException(400, "No shareholder column found - provide a shareholder name")

    added, errors = 0, []
    with db() as con:
        cache: dict[str, int] = {}
        for i, row in enumerate(reader, start=2):
            try:
                if not any((v or "").strip() for v in row.values()):
                    continue
                name = (row.get(sh_col) if sh_col else "") or shareholder_name
                name = name.strip().title()
                if name not in cache:
                    con.execute(
                        "INSERT INTO shareholders(name) VALUES (:name) ON CONFLICT (lower(name)) DO NOTHING",
                        {"name": name},
                    )
                    cache[name] = con.execute(
                        "SELECT id FROM shareholders WHERE lower(name)=lower(:name)", {"name": name}
                    ).fetchone()["id"]
                d = parse_date(row[cols["date"]], default_year)
                con.execute(
                    """INSERT INTO entries(shareholder_id, entry_date, purpose, amount)
                       VALUES (:sid, :d, :purpose, :amount)""",
                    {"sid": cache[name], "d": d, "purpose": row[purpose_col].strip(),
                     "amount": to_paise(parse_amount(row[cols["amount"]]))},
                )
                added += 1
            except Exception as exc:  # report and continue
                errors.append(f"Row {i}: {exc}")
    return {"added": added, "errors": errors}


@app.get("/api/export.csv")
def export_csv(shareholder_id: int | None = None):
    sql = """SELECT e.entry_date, e.purpose, e.amount, s.name FROM entries e
             JOIN shareholders s ON s.id = e.shareholder_id"""
    params: dict = {}
    if shareholder_id:
        sql += " WHERE s.id = :shareholder_id"
        params["shareholder_id"] = shareholder_id
    sql += " ORDER BY s.name, e.entry_date, e.id"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "purpose", "amount", "shareholder"])
    with db() as con:
        for r in con.execute(sql, params).fetchall():
            w.writerow([r["entry_date"], r["purpose"], f"{r['amount'] / 100:.2f}", r["name"]])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ledger.csv"},
    )


# ---------- static UI ----------
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
