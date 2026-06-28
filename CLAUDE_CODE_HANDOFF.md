# Portfolio Tracker — Handoff Brief

## What this is

Adding a **portfolio tracking** feature to the existing `magicformula` Flask
app. The data layer is already built, tested, and dropped into the project as
`portfolio.py`. Your job is the **web layer**: Flask routes in `app.py`, a
portfolio tab in the template, and the add/edit/delete UI.

The design decisions below are **already settled** — please don't reopen them.
Build to this spec. If something seems genuinely wrong, flag it, but default to
executing what's here.

---

## Project conventions to match (don't invent new patterns)

- The app is a single `app.py` with routes defined flat at module level
  (no blueprints). Portfolio routes go in `app.py` too, same as the existing
  Single Ticker and Scan routes.
- DB access goes through `magicformula.get_conn()` (WAL mode, busy_timeout,
  `row_factory = sqlite3.Row`). `portfolio.py` already does this — match it.
- The three logic modules are: `magicformula.py` (core MF + single-ticker),
  `deep_scan.py` (full-universe ranking), `portfolio.py` (NEW — portfolio
  tracking). `app.py` is the Flask orchestration layer over all three.
- **Match the existing tab mechanism and route-return style** (HTML fragment
  vs. JSON rendered client-side). Read how the Single Ticker tab does it and
  follow the same approach — that's the closest analog to portfolio CRUD.
  Do not introduce a different frontend pattern.

---

## The two new files (already in the project, already tested)

- `portfolio.py` — schema + CRUD + Merrill CSV parser. All functions go
  through `mf.get_conn()`.
- `test_portfolio.py` — end-to-end smoke test against a throwaway copy of
  `cache.db`. Run it after your changes as a regression check:
  `python test_portfolio.py Holdings_05282020.csv` (or with no arg to skip
  the import path). It must stay green.

### `portfolio.py` public functions
- `init_schema()` — creates `portfolios` + `positions` tables (idempotent)
- `create_portfolio(name, user_id=None) -> id`
- `list_portfolios()`, `get_portfolio(id)`, `delete_portfolio(id)` (cascades),
  `rename_portfolio(id, new_name)`
- `add_position(portfolio_id, ticker, shares, cost_basis=None, acquired_date=None) -> id`
- `bulk_add_positions(portfolio_id, rows)` — CSV bootstrap insert
- `list_positions(portfolio_id)`, `update_position(position_id, ...)`,
  `delete_position(position_id)`
- `parse_merrill_csv(file_or_path) -> (rows, skipped)` — accepts a path OR a
  file-like object (Flask upload stream)
- `import_merrill_csv(portfolio_id, file_or_path) -> (count, skipped)` —
  parse + bulk insert in one call

---

## Schema (already created by init_schema; do not alter)

```sql
CREATE TABLE portfolios (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    user_id INTEGER,                 -- nullable, reserved for future multi-tenancy
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE positions (
    id INTEGER PRIMARY KEY,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    ticker TEXT NOT NULL,
    shares REAL,
    cost_basis REAL,                 -- PER SHARE (total lot = cost_basis * shares)
    acquired_date DATE               -- null from Merrill; manual entry only
);
```

---

## Settled design decisions (do NOT re-litigate)

1. **CSV import is bootstrap-only.** It runs ONCE at portfolio creation to seed
   holdings. It is NOT a recurring sync. `bulk_add_positions` is a plain INSERT,
   not an upsert — re-importing into the same portfolio would duplicate rows, by
   design. Don't add merge/upsert/dedup logic.
2. **Two ingestion paths from day one:** CSV import (real brokerage bootstrap)
   AND manual entry (paper portfolios, post-bootstrap edits). A CSV-sourced row
   and a manually-added row are identical once inserted.
3. **`cost_basis` is per-share**, derived from Merrill as
   `(Value - Unrealized G/L) / Quantity`, null when G/L is `--`. Total lot cost
   is a display-time derivation (`cost_basis * shares`), not stored.
4. **Position removal is a plain DELETE** (the `-`/`x` button → `delete_position`).
   No soft-delete, no `is_excluded` flag, no exclusion table.
5. **Index/benchmark ETF exclusion (QQQ, EWS) is MANUAL**, via that delete
   button, done once post-import. Auto-classifying ETFs is explicitly deferred
   as scope creep — do NOT build it.
6. **Fund/zero-qty filtering is automatic at parse time** (already in
   `portfolio.py`): fractional quantity → fund → skip; zero quantity →
   sold-out → skip. The `skipped` list reports what was dropped and why.
7. **No fractional-share features.** Whole shares only for stocks; the
   fractional filter doubles as the fund detector.

---

## Your two integration steps

1. **Call `portfolio.init_schema()` at app startup**, wherever the existing
   `company_cache` / `mf_universe` / `raw_json_vault` tables get their
   `CREATE TABLE IF NOT EXISTS`. It's idempotent; safe on every boot.
2. **Surface the `skipped` list in the import route's response.** When the user
   imports a CSV, the response must tell them "imported N, skipped X (fund),
   Y (sold-out)" — otherwise the filtering is invisible and they'll wonder why
   the import is short rows. Don't swallow it.

---

## Routes to build (suggested shape — match existing app.py conventions)

- `GET  /portfolios` — list all portfolios
- `POST /portfolios` — create a portfolio (name; optional CSV upload to
  bootstrap; returns id + import summary incl. skipped list)
- `GET  /portfolio/<id>` — portfolio detail / positions view
- `POST /portfolio/<id>/position` — manual add one position
- `PUT|POST /position/<id>` — edit a position (shares/cost_basis/acquired_date)
- `DELETE|POST /position/<id>/delete` — delete a position (the -/x button)
- `POST /portfolio/<id>/delete` — delete whole portfolio (cascades)

(Use whatever verb/return convention the existing routes use — match, don't
impose REST purity if the app uses POST-for-everything + JSON.)

---

## Frontend (match the existing tab pattern)

- Add a **Portfolio tab** alongside the existing tabs (Scan, Single Ticker).
  Use the same tab-switching + active-tab-persistence approach already in
  `index.html` (memory says localStorage-persisted active tab — confirm in the
  actual file).
- Portfolio detail view: a table of positions (ticker, shares, per-share cost,
  and — derived client-side — total lot cost; live value/return can come later
  once you wire in current prices from the existing FMP/ticker plumbing).
- Each position row has a **`-` / `x` button** → delete.
- A **`+` / "Add position"** control → manual add (ticker, shares, optional
  cost basis).
- Portfolio creation: name field + optional CSV file upload. On upload, show
  the import summary including skipped rows.

---

## Explicitly NOT now (deferred — do not build)

- **Backtest / return-since-purchase.** Schema already supports it
  (`acquired_date` + `cost_basis`); it's a later session.
- **Auto-excluding index/benchmark ETFs.** Manual delete for now.
- **Re-import / rebalancing / reconciliation.** Bootstrap-only stands.
- **Multi-tenancy machinery.** `portfolios.user_id` is reserved-but-unused;
  don't build auth/permissions around it.

---

## Definition of done for this session

- `portfolio.init_schema()` wired into startup.
- Portfolio routes live in `app.py`, matching existing conventions.
- Portfolio tab in the template: list portfolios, view one, add/delete
  positions, create-with-optional-CSV showing the skipped summary.
- `python test_portfolio.py Holdings_05282020.csv` still passes.
- Local test (the app's existing local-run flow) before any commit/deploy.
  The project's deploy flow is: `git add -A && git commit && git push`, then on
  the droplet `git pull && sudo systemctl restart magicformula`. Don't deploy
  unprompted — leave that to the user.
