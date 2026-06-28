"""
portfolio.py

Portfolio tracking module for magicformula.

Owns the `portfolios` and `positions` tables and all CRUD operations
against them. Routes in app.py should be thin wrappers that call into
this module rather than touching sqlite directly.

Uses magicformula.get_conn() for connections, consistent with
deep_scan.py's pattern (WAL mode, busy_timeout, row_factory all
inherited from there).
"""

import csv
import io
import json
import os
import re

import magicformula as mf


# ---------------------------------------------------------------------------
# Merrill CSV import
# ---------------------------------------------------------------------------
#
# Bootstrap-only: parses a Merrill Edge "ExportData" holdings export into
# rows ready for bulk_add_positions(). NOT a recurring sync.
#
# The export format has a multi-line preamble (timestamp, account name,
# summary/gain-loss block) before the column header row, so we locate the
# header by scanning rather than assuming a fixed line count.

_FOOTER_LABELS = frozenset({
    "balances",
    "money accounts",
    "cash balance",
    "pending activity",
    "margin balance",
    "total",
    "account total",
})

_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")


def _clean_number(raw):
    """
    Convert a Merrill numeric cell to float, or None.

    Handles '--' (Merrill null), leading '$', comma thousands separators,
    parenthesised accounting negatives e.g. '(187.28)' -> -187.28, and
    combined cells like '-- --' or '$0.00 0.00%' by taking the first token.
    """
    if raw is None:
        return None
    tokens = raw.strip().split()
    if not tokens:
        return None
    s = tokens[0].lstrip("$")
    if not s or s == "--":
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace(",", "")
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def _clean_symbol(raw):
    """Strip whitespace, quotes, and a trailing Merrill '!' flag; uppercase."""
    s = raw.strip().strip('"').strip()
    if s.endswith("!"):
        s = s[:-1].strip()
    return s.upper()


def parse_merrill_csv(file_or_path):
    """
    Parse a Merrill Edge ExportData holdings export.

    Accepts either a filesystem path (str) or a file-like object (e.g. a
    Flask upload's stream). Returns (rows, skipped) where:
      - rows is a list of dicts ready for bulk_add_positions():
          {ticker, shares, cost_basis (per-share or None), acquired_date}
      - skipped is a list of (symbol, reason) tuples so silent drops are
        visible to the caller.

    cost_basis priority: Cost Basis column total / qty, then
    (Value - Unrealized G/L) / qty, then None. In current exports Cost Basis
    is '--' throughout, so None is the expected result — not a bug.
    """
    if hasattr(file_or_path, "read"):
        data = file_or_path.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8-sig")
        all_lines = io.StringIO(data).readlines()
    else:
        with open(file_or_path, newline="", encoding="utf-8-sig") as f:
            all_lines = f.readlines()

    # Locate the header row: first line whose first CSV cell stripped == "symbol"
    header_idx = None
    for i, line in enumerate(all_lines):
        cells = next(csv.reader([line]), None)
        if cells and cells[0].strip().lower() == "symbol":
            header_idx = i
            break

    if header_idx is None:
        return [], [("", "header row not found in file")]

    body = io.StringIO("".join(all_lines[header_idx:]))
    reader = csv.reader(body)

    raw_header = next(reader)
    col = {h.strip(): i for i, h in enumerate(raw_header)}

    sym_i = col.get("Symbol")
    qty_i = col.get("Quantity")
    price_i = col.get("Price")
    cb_i = col.get("Cost Basis")
    val_i = col.get("Value")
    gl_i = col.get("Unrealized Gain/Loss $ Chg % Chg")

    def cell(row, idx):
        return row[idx] if idx is not None and idx < len(row) else None

    rows, skipped = [], []

    for r in reader:
        if not r or not any(c.strip() for c in r):
            continue

        symbol = _clean_symbol(cell(r, sym_i) or "")
        if not symbol:
            skipped.append(("", "no symbol"))
            continue

        if symbol.lower() in _FOOTER_LABELS:
            skipped.append((symbol, "footer row"))
            continue

        if not _TICKER_RE.match(symbol):
            raw_sym = (cell(r, sym_i) or "").strip()
            skipped.append((symbol, f"non-ticker symbol: {raw_sym}"))
            continue

        qty = _clean_number(cell(r, qty_i))
        if qty is None:
            skipped.append((symbol, "unparseable quantity"))
            continue
        if qty == 0:
            skipped.append((symbol, "zero quantity (sold-out position)"))
            continue
        if not float(qty).is_integer():
            skipped.append((symbol, f"fractional quantity {qty} (fund, not a stock)"))
            continue

        price = _clean_number(cell(r, price_i))
        value = _clean_number(cell(r, val_i))

        if (price is not None and price == 0) or (value is not None and value == 0):
            skipped.append((symbol, "zero price/value (dead position)"))
            continue

        cb_total = _clean_number(cell(r, cb_i))
        gl = _clean_number(cell(r, gl_i))

        if cb_total is not None:
            cost_basis = cb_total / qty
        elif value is not None and gl is not None:
            cost_basis = (value - gl) / qty
        else:
            cost_basis = None

        rows.append({
            "ticker": symbol,
            "shares": int(qty),
            "cost_basis": cost_basis,
            "acquired_date": None,
        })

    return rows, skipped


def import_merrill_csv(portfolio_id, file_or_path):
    """
    Convenience: parse a Merrill export and bulk-insert into a portfolio in
    one call. Returns (imported_count, skipped) so the route can report both
    "added N positions" and "skipped these and why".

    Bootstrap-only by intent — running it twice against the same portfolio
    will duplicate rows (bulk_add_positions is a plain INSERT, not an upsert).
    """
    rows, skipped = parse_merrill_csv(file_or_path)
    if rows:
        bulk_add_positions(portfolio_id, rows)
    return len(rows), skipped


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_schema():
    """
    Create the portfolios and positions tables if they don't exist.
    Call this once at app startup, the same way other table creation
    happens (alongside company_cache / mf_universe / raw_json_vault).
    """
    conn = mf.get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolios (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY,
                portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
                ticker TEXT NOT NULL,
                shares REAL,
                cost_basis REAL,
                acquired_date DATE
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_positions_portfolio_id
            ON positions(portfolio_id)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY,
                portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
                snapshot_date DATE NOT NULL,
                total_value REAL,
                total_cost_basis REAL,
                spy_price REAL,
                qqq_price REAL,
                dia_price REAL,
                iwm_price REAL,
                UNIQUE(portfolio_id, snapshot_date)
            )
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Portfolios
# ---------------------------------------------------------------------------

def create_portfolio(name, user_id=None):
    """Create a new (empty) portfolio. Returns the new portfolio's id."""
    conn = mf.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO portfolios (name, user_id) VALUES (?, ?)",
            (name, user_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_portfolios():
    """Return all portfolios as a list of dict-like rows."""
    conn = mf.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM portfolios ORDER BY created_at DESC"
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_portfolio(portfolio_id):
    """Return a single portfolio row, or None if it doesn't exist."""
    conn = mf.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)
        ).fetchone()
        return row
    finally:
        conn.close()


def delete_portfolio(portfolio_id):
    """Delete a portfolio and all of its positions."""
    conn = mf.get_conn()
    try:
        conn.execute("DELETE FROM positions WHERE portfolio_id = ?", (portfolio_id,))
        conn.execute("DELETE FROM portfolios WHERE id = ?", (portfolio_id,))
        conn.commit()
    finally:
        conn.close()


def rename_portfolio(portfolio_id, new_name):
    conn = mf.get_conn()
    try:
        conn.execute(
            "UPDATE portfolios SET name = ? WHERE id = ?",
            (new_name, portfolio_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def add_position(portfolio_id, ticker, shares, cost_basis=None, acquired_date=None):
    """
    Add a single position to a portfolio. Used for both manual entry
    (paper portfolios, post-import additions) and as the per-row insert
    target for CSV bulk import.
    Returns the new position's id.
    """
    conn = mf.get_conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO positions (portfolio_id, ticker, shares, cost_basis, acquired_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (portfolio_id, ticker.upper(), shares, cost_basis, acquired_date),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def bulk_add_positions(portfolio_id, rows):
    """
    Insert many positions at once — the CSV bootstrap-import path.
    `rows` is a list of dicts with keys: ticker, shares, cost_basis (optional),
    acquired_date (optional).

    This is a one-time bulk insert intended for portfolio creation, not an
    upsert — re-running it against the same portfolio will duplicate rows.
    """
    conn = mf.get_conn()
    try:
        conn.executemany(
            """
            INSERT INTO positions (portfolio_id, ticker, shares, cost_basis, acquired_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    portfolio_id,
                    r["ticker"].upper(),
                    r.get("shares"),
                    r.get("cost_basis"),
                    r.get("acquired_date"),
                )
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def list_positions(portfolio_id):
    """Return all positions for a given portfolio."""
    conn = mf.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM positions WHERE portfolio_id = ? ORDER BY ticker",
            (portfolio_id,),
        ).fetchall()
        return rows
    finally:
        conn.close()


def update_position(position_id, shares=None, cost_basis=None, acquired_date=None):
    """
    Hand-edit a position's mutable fields (e.g. DRIP-driven share count drift).
    Only updates fields that are explicitly passed.
    """
    fields, values = [], []
    if shares is not None:
        fields.append("shares = ?")
        values.append(shares)
    if cost_basis is not None:
        fields.append("cost_basis = ?")
        values.append(cost_basis)
    if acquired_date is not None:
        fields.append("acquired_date = ?")
        values.append(acquired_date)

    if not fields:
        return  # nothing to update

    values.append(position_id)
    conn = mf.get_conn()
    try:
        conn.execute(
            f"UPDATE positions SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def delete_position(position_id):
    """Remove a single position — this is what the - / x button calls."""
    conn = mf.get_conn()
    try:
        conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Portfolio snapshots
# ---------------------------------------------------------------------------

_BENCHMARKS = ["SPY", "QQQ", "DIA", "IWM"]


def _fetch_benchmark_prices():
    """Return {ticker: float} for SPY/QQQ/DIA/IWM, vault-first, FMP fallback.

    A failed fetch for any benchmark stores nothing for that ticker so the
    caller gets None rather than raising.
    """
    result = {}
    conn = mf.get_conn()
    try:
        rows = conn.execute(
            "SELECT ticker, json_blob FROM raw_json_vault"
            " WHERE ticker IN (?,?,?,?) AND endpoint = 'quote'",
            _BENCHMARKS,
        ).fetchall()
        for row in rows:
            try:
                blob = json.loads(row["json_blob"])
                q = blob[0] if isinstance(blob, list) and blob else (blob if isinstance(blob, dict) else {})
                p = q.get("price")
                if p is not None:
                    result[row["ticker"]] = float(p)
            except Exception:
                pass

        missing = [t for t in _BENCHMARKS if t not in result]
        if missing:
            api_key = os.getenv("FMP_API_KEY", "")
            if api_key:
                try:
                    data = mf.fmp_get(f"/quote/{','.join(missing)}", api_key=api_key)
                    if data and isinstance(data, list):
                        for q in data:
                            ticker = (q.get("symbol") or "").upper()
                            price = q.get("price")
                            if ticker in _BENCHMARKS and price is not None:
                                result[ticker] = float(price)
                                conn.execute(
                                    """INSERT INTO raw_json_vault (ticker, endpoint, json_blob)
                                       VALUES (?, ?, ?)
                                       ON CONFLICT(ticker, endpoint) DO UPDATE SET
                                           json_blob = excluded.json_blob,
                                           last_updated = CURRENT_TIMESTAMP""",
                                    (ticker, "quote", json.dumps([q])),
                                )
                        conn.commit()
                except Exception as e:
                    print(f"[record_snapshot] benchmark fetch error: {e}")
    finally:
        conn.close()
    return result


def record_snapshot(portfolio_id, prices=None):
    """Compute and upsert today's portfolio snapshot.

    prices: {TICKER: {"price": float, ...}} from _fetch_portfolio_prices, or
    a plain {TICKER: float} mapping. Pass None if no current prices are
    available — total_value will be stored as None.

    Benchmark prices (SPY/QQQ/DIA/IWM) are fetched here, vault-first with an
    FMP fallback. A missing benchmark stores NULL for that column rather than
    aborting the whole snapshot.
    """
    positions = list_positions(portfolio_id)

    total_cost_basis = None
    for pos in positions:
        if pos["cost_basis"] is not None:
            if total_cost_basis is None:
                total_cost_basis = 0.0
            total_cost_basis += pos["cost_basis"] * pos["shares"]

    total_value = None
    if prices is not None:
        running = 0.0
        for pos in positions:
            info = prices.get(pos["ticker"])
            if info is None:
                running = None
                break
            price_val = info["price"] if isinstance(info, dict) else float(info)
            running += price_val * pos["shares"]
        total_value = running

    benchmarks = _fetch_benchmark_prices()

    conn = mf.get_conn()
    try:
        conn.execute(
            """
            INSERT INTO portfolio_snapshots
                (portfolio_id, snapshot_date, total_value, total_cost_basis,
                 spy_price, qqq_price, dia_price, iwm_price)
            VALUES (?, CURRENT_DATE, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(portfolio_id, snapshot_date) DO UPDATE SET
                total_value      = excluded.total_value,
                total_cost_basis = excluded.total_cost_basis,
                spy_price        = excluded.spy_price,
                qqq_price        = excluded.qqq_price,
                dia_price        = excluded.dia_price,
                iwm_price        = excluded.iwm_price
            """,
            (
                portfolio_id,
                total_value,
                total_cost_basis,
                benchmarks.get("SPY"),
                benchmarks.get("QQQ"),
                benchmarks.get("DIA"),
                benchmarks.get("IWM"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_snapshots(portfolio_id):
    """Return all snapshots for a portfolio ordered by snapshot_date ascending."""
    conn = mf.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots"
            " WHERE portfolio_id = ? ORDER BY snapshot_date ASC",
            (portfolio_id,),
        ).fetchall()
        return rows
    finally:
        conn.close()
