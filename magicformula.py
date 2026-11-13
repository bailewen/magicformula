#!/usr/bin/env python3
"""
Magic Formula screener
Based on Joel Greenblatt's strategy, using Financial Modeling Prep (FMP) data.
-------------------------------------------------
This code uses Financial Modeling Prep (FMP).
Starter Annual Plan

Requirements:
  Python 3.10 or later
  python -m venv venv && source venv/bin/activate    # Assumes Linux environment
  pip install -r requirements.txt

Environment:
  export FMP_API_KEY="YOUR_KEY"

Notes:
- Symbols come from FMP's /stock-screener, one call per exchange (active
  stocks only; ETFs, funds, warrants, preferreds and sub-minimum market
  caps are filtered out).
- Profile (market cap, country, sector, industry) comes from /profile/{ticker}.
- Income statement: /income-statement/{ticker}. TTM mode (default) uses the
  last 4 quarters; --annual uses the latest annual statement.
- Balance sheet: /balance-sheet-statement/{ticker}, latest quarter.
- EBIT = operatingIncome (TTM: sum of the last 4 quarters).
- EY  = EBIT / EV
    EV = marketCap + totalDebt - cashAndShortTermInvestments
    (totalDebt falls back to shortTermDebt + longTermDebt if missing)
- ROC = EBIT / Capital
    Capital = NWC + net PP&E (propertyPlantEquipmentNet)
    NWC = (totalCurrentAssets - cash & short term investments)
          - (totalCurrentLiabilities - shortTermDebt), floored at 0
    Goodwill and intangibles are excluded unless --goodwill / --intangibles.
- Rank: MF_score = EY rank + ROC rank (lower is better).
- Excludes financials, utilities and real estate, and skips tickers with
  negative EBIT, non-positive EV, capital under $10M, or implausible data.
- Results are cached in cache.db for 7 days, keyed by ticker and options.

Caveats:
- FMP field names can differ by company and period. If a required field is
  missing, the ticker is skipped and the reason is listed in the skipped CSV.

Run:
  python magicformula.py --top 30 --limit 0      # full universe
  python magicformula.py --top 30 --limit 400    # quick test run
 
2.0 cleaned up references to Finnhub & adapted rate limits to FMP paid tier, added parameters to fmp_get() to filter for only actual stocks
2.2 switched to TTM and added debugging visibility (error messages)
"""
from __future__ import annotations
import os, time, argparse
from typing import Dict, Any, List, Optional
import requests
import pandas as pd
import datetime
import sqlite3
import json
from pathlib import Path
from collections import deque
import threading

#---rate limiter
class RateLimiter:
    """Simple per-minute rate limiter."""
    def __init__(self, calls_per_minute=300):
        self.calls_per_minute = calls_per_minute
        self.window = 60.0
        self.calls = deque()
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > self.window:
                    self.calls.popleft()
                if len(self.calls) < self.calls_per_minute:
                    self.calls.append(now)
                    return
                sleep_for = self.window - (now - self.calls[0]) + 0.01
            time.sleep(sleep_for)
            
limiter = RateLimiter(calls_per_minute=300)

try:
    from tqdm import tqdm  # progress bar (optional)
except Exception:  # pragma: no cover
    def tqdm(x, **_):
        return x

FMP_BASE = "https://financialmodelingprep.com/api/v3"
FMP_KEY  = os.getenv("FMP_API_KEY", "")

# Industries excluded even though FMP files them under a non-financial sector.
# Health insurers hold large cash/investment portfolios against claims, which
# distorts EV and capital the same way it does for other financials.
EXCLUDE_INDUSTRIES = {
    "Medical - Healthcare Plans",
}

# If exactly one of the four TTM quarters is an outlier (|operating income| >
# its revenue, e.g. an impairment or one-off gain) and the other three are all
# profitable, replace the outlier with the average of the other three instead
# of letting it swamp (or discard) the whole year. Set False to disable.
NORMALIZE_ONE_OFF_QUARTERS = True

# Skip companies whose most recent quarter shows an operating loss, even if
# trailing-12-month EBIT is positive (a one-off gain can mask ongoing losses).
# Greenblatt uses trailing-12-month EBIT only, so set False to match that.
REQUIRE_PROFITABLE_LATEST_QUARTER = False

# Cap ROC when ranking: any ROC above this counts as equal (tied for best ROC
# rank), so EY decides among them. The displayed ROC stays uncapped.
# Set to None to rank on raw ROC (Greenblatt's original behavior).
ROC_RANK_CAP = 1.0   # 100%; None to rank on raw ROC

# Treat any ROC above this as suspicious and skip the ticker entirely
# (previously a hard-coded 10.0 = 1000% "data error" check).
# Set to None to disable; 10.0 restores the old behavior.
ROC_EXCLUDE_ABOVE = None   # e.g. 10.0 = 1000%, the original data-error check

# Capital (the ROC denominator) is never allowed below this: a company with
# almost no tangible capital is ranked as if it had CAPITAL_FLOOR instead of
# being skipped, so ROC stays finite (and, with ROC_RANK_CAP, ties for best).
CAPITAL_FLOOR = 10e6   # $10M

EXCLUDE_SECTORS = {
    "Financial Services",
    "Financial",
    "Banks",
    "Insurance",
    "Utilities",
    "Utility",
    "Real Estate",
    "Real Estate Investment Trust",
    "REIT",}

# -------------------- HTTP helpers --------------------

S = requests.Session()
S.headers.update({"User-Agent": "MagicFormula (+https://magic.omarbelove.com)"})

def fmp_get(path: str, params: Optional[Dict[str, Any]] = None, retries: int = 3, backoff: float = 0.8, api_key: str = None):
    if params is None:
        params = {}

    if not api_key:
        api_key = os.getenv("FMP_API_KEY", "")
    if not api_key:
        raise RuntimeError("FMP_API_KEY not set. Provide it directly or set FMP_API_KEY environment variable")
 
    params = dict(params)
    params["apikey"] = api_key
    url = f"{FMP_BASE}{path}"
    for a in range(retries):
        limiter.wait()
        r = S.get(url, params=params, timeout=5)
        if r.status_code == 429:
            time.sleep(backoff * (a + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise requests.HTTPError(f"Failed after retries: {url}")

# -------------------- Data pulling --------------------

def list_symbols(ex: str, min_mcap: float = 50e6, countries: List[str] = None, api_key: str = None) -> List[Dict[str, Any]]:
    """Return only active US common stocks above min_mcap."""
    
    rows = fmp_get(
    "/stock-screener",
    {
        "exchange": ex,
        "isEtf": False,
        "isFund": False,
        "isActivelyTrading": True,
        "limit": 10000,
    },
    api_key=api_key
    ) or []
    
    filtered = []
    for r in rows:
        sym = r.get("symbol")
        mcap = r.get("marketCap") or 0

        # --- filter out ETFs, funds, warrants, preferreds stocks, SPACs, microcaps ---
        if (
            sym
            and isinstance(sym, str)
            and (countries is None or r.get("country") in countries)
            and not any(sym.endswith(x) for x in ("WT", "WS", "PR"))
            and "-" not in sym
            and sym.isalpha()
            and len(sym) <= 5
            and mcap >= min_mcap
        ):
            filtered.append(r)

    return filtered

def check_financial_health(symbol: str,
    check_debt_revenue: bool = False,
    check_cashflow_quality: bool = False,
    debt_revenue_quarters: int = 6,
    cashflow_quarters: int = 8,
    api_key: str = None) -> dict:
    """
    Optional health checks.
    Returns dict with pass/fail for each enabled check.
    """
    results = {"symbol": symbol, "passes_all": True}
    
    # Check 1: D/E decreasing while revenue increasing
    if check_debt_revenue:
        try:
            bs_data = fmp_get(f"/balance-sheet-statement/{symbol}", 
                             {"period": "quarter", "limit": debt_revenue_quarters}, api_key=api_key)
            is_data = fmp_get(f"/income-statement/{symbol}", 
                             {"period": "quarter", "limit": debt_revenue_quarters}, api_key=api_key)
            
            if bs_data and is_data and len(bs_data) >= 3 and len(is_data) >= 3:
                # Calculate D/E ratios (oldest to newest)
                de_ratios = []
                for q in reversed(bs_data):
                    equity = q.get("totalStockholdersEquity") or 0
                    debt = q.get("totalDebt") or 0
                    de_ratios.append(debt / equity if equity > 0 else float('inf'))
                
                revenues = [q.get("revenue") or 0 for q in reversed(is_data)]
                
                # Check trend: D/E should trend down, revenue should trend up
                de_decreasing = all(de_ratios[i] >= de_ratios[i+1] for i in range(len(de_ratios)-1))
                rev_increasing = all(revenues[i] <= revenues[i+1] for i in range(len(revenues)-1))
                
                results["debt_revenue_check"] = de_decreasing and rev_increasing
            else:
                results["debt_revenue_check"] = None  # Insufficient data
        except Exception as e:
            print(f"[check_financial_health] {symbol} debt/revenue check: {type(e).__name__}: {e}", flush=True)
            results["debt_revenue_check"] = None
        
        if results.get("debt_revenue_check") is False:
            results["passes_all"] = False
    
    # Check 2: OCF > Net Income for consecutive quarters
    if check_cashflow_quality:
        try:
            cf_data = fmp_get(f"/cash-flow-statement/{symbol}", 
                             {"period": "quarter", "limit": cashflow_quarters}, api_key=api_key)
            
            if cf_data and len(cf_data) >= 4:
                ocf_beats_ni = all(
                    (q.get("operatingCashFlow") or 0) > (q.get("netIncome") or 0)
                    for q in cf_data
                )
                results["cashflow_quality_check"] = ocf_beats_ni
            else:
                results["cashflow_quality_check"] = None
        except Exception as e:
            print(f"[check_financial_health] {symbol} cashflow check: {type(e).__name__}: {e}", flush=True)
            results["cashflow_quality_check"] = None
        
        if results.get("cashflow_quality_check") is False:
            results["passes_all"] = False
    
    return results

def _latest(items: List[Dict[str, Any]], field: str):
    if not items:
        return None
    v = items[0].get(field)
    return None if v in (None, "") else float(v)

# -------------------- Income-statement sanity checks --------------------
def _check_income_quality(periods: list, require_quarters: bool,
                          skip_idx: Optional[int] = None) -> Optional[str]:
    """
    Sanity-check the income-statement periods that feed EBIT.
    Returns a skip reason string, or None if the data looks usable.

    - A period whose operating income exceeds its revenue (margin > 100%) is
      almost always a one-off gain booked in operatingIncome (or a data
      error); it would badly inflate EBIT, EY and ROC.
    - For TTM (require_quarters), the periods must be four distinct,
      consecutive quarters - not annual rows or stale/duplicated data.
    """
    for i, q in enumerate(periods):
        if i == skip_idx:
            continue  # normalized one-off quarter, handled by _ttm_ebit
        oi = q.get("operatingIncome")
        rev = q.get("revenue")
        if oi is not None and rev is not None and rev > 0 and oi > rev:
            return (f"Operating income exceeds revenue in {q.get('date', '?')} "
                    f"({oi:,.0f} vs {rev:,.0f}) - likely one-off gain/data error")

    if require_quarters:
        for q in periods:
            per = q.get("period")
            if per is not None and not str(per).upper().startswith("Q"):
                return f"Non-quarterly period '{per}' in TTM data (stale vault row?)"
        try:
            dates = [datetime.datetime.strptime(q["date"], "%Y-%m-%d") for q in periods]
        except Exception:
            return None  # no usable dates - can't verify; other checks still apply
        if len(set(dates)) != len(dates):
            return "Duplicate quarter dates in TTM data"
        for newer, older in zip(dates, dates[1:]):
            gap = (newer - older).days
            if not 60 <= gap <= 125:
                return f"Quarters not consecutive (gap of {gap} days)"
    return None

def _one_off_quarter(quarters: list) -> Optional[int]:
    """Index of a single outlier quarter (|operating income| > revenue) when the
    other three quarters are all profitable; otherwise None."""
    out = [i for i, q in enumerate(quarters)
           if (q.get("revenue") or 0) > 0 and q.get("operatingIncome") is not None
           and abs(q["operatingIncome"]) > q["revenue"]]
    if len(out) != 1:
        return None
    others = [q.get("operatingIncome") for j, q in enumerate(quarters) if j != out[0]]
    if any(o is None or o <= 0 for o in others):
        return None
    return out[0]


def _ttm_ebit(quarters: list):
    """Trailing-four-quarter EBIT. Returns (ebit, None) or (None, skip_reason)."""
    one_off = _one_off_quarter(quarters) if NORMALIZE_ONE_OFF_QUARTERS else None
    bad = _check_income_quality(quarters, require_quarters=True, skip_idx=one_off)
    if bad:
        return None, bad
    ois = [q.get("operatingIncome") or 0 for q in quarters]
    if one_off is not None:
        others = [o for j, o in enumerate(ois) if j != one_off]
        ois[one_off] = sum(others) / len(others)
    if REQUIRE_PROFITABLE_LATEST_QUARTER and ois[0] <= 0:
        return None, "Operating loss in most recent quarter"
    return sum(ois), None


# -------------------- Field helpers --------------------
def _compute_mf_metrics(prof: dict, inc: list, bal: list, annual: bool, include_goodwill: bool = False,
                            include_intangibles: bool = False) -> Optional[Dict[str, Any]]:
    """
    Core MF computation from pre-parsed data structures.
    Returns a result dict on success, or a dict with 'reason' key on skip, or None.
    """
    company_name = prof.get("companyName") or prof.get("company") or prof.get("symbol", "")

    if prof.get("sector") in EXCLUDE_SECTORS:
        return None

    if prof.get("industry") in EXCLUDE_INDUSTRIES:
        return None

    name = prof.get("companyName", "").lower()
    if any(x in name for x in ["preferred", "perpetual", "series a", "series b"]):
        return None

    if annual:
        bad = _check_income_quality(inc[:1], require_quarters=False)
        if bad:
            return {"reason": bad}
        ebit = _latest(inc, "operatingIncome")
    elif inc and len(inc) >= 4:
        ebit, bad = _ttm_ebit(inc[:4])
        if bad:
            return {"reason": bad}
    else:
        ebit = None

    tca  = _latest(bal, "totalCurrentAssets")
    tcl  = _latest(bal, "totalCurrentLiabilities")
    ppe  = _latest(bal, "propertyPlantEquipmentNet")
    cash = _latest(bal, "cashAndShortTermInvestments")
    debt = _latest(bal, "totalDebt")
    if debt is None:
        debt = (_latest(bal, "shortTermDebt") or 0.0) + (_latest(bal, "longTermDebt") or 0.0)
    short_debt = _latest(bal, "shortTermDebt") or 0.0

    # Sanity check: reported totalDebt can't be smaller than its own components.
    # (FMP has briefly served balance sheets with long-term debt missing, which
    # understates EV and inflates EY - e.g. XPOF on 2026-11-10.)
    long_debt = _latest(bal, "longTermDebt") or 0.0
    if debt is not None and (short_debt + long_debt) > debt * 1.01 + 1e6:
        return {"reason": (f"totalDebt ({debt:,.0f}) below short+long-term debt "
                           f"({short_debt + long_debt:,.0f}) - inconsistent balance sheet")}
 
    mcap = prof.get("mktCap") if prof.get("mktCap") is not None else prof.get("marketCap")
    try:
        mcap = float(mcap) if mcap is not None else None
    except Exception:
        mcap = None

    if None in (ebit, tca, tcl, ppe, cash, mcap):
        missing = [n for n, v in [("ebit", ebit), ("tca", tca), ("tcl", tcl),
                                   ("ppe", ppe), ("cash", cash), ("mcap", mcap)] if v is None]
        return {"reason": f"Missing fields: {', '.join(missing)}"}

    ev = mcap + debt - cash
    nwc = (tca - cash) - (tcl - short_debt)
    nwc = max(nwc, 0)
    goodwill = _latest(bal, "goodwill") or 0
    intangibles = _latest(bal, "intangibleAssets") or 0
                             
    if not include_goodwill:
        goodwill = 0
    if not include_intangibles:
        intangibles = 0
    capital = nwc + ppe + goodwill + intangibles

    if ev <= 0:
        return {"reason": "Negative or zero EV"}
    if ev < mcap * 0.01:
        return {"reason": "EV implausibly small vs market cap (data error)"}
    capital = max(capital, CAPITAL_FLOOR)
    if ebit < 0:
        return {"reason": "Negative EBIT"}

    ey  = ebit / ev
    roc = ebit / capital

    if ROC_EXCLUDE_ABOVE is not None and roc > ROC_EXCLUDE_ABOVE:
        return {"reason": f"ROC > {ROC_EXCLUDE_ABOVE:.0%} (suspicious)"}

    return {
        "name":      company_name,
        "exchange":  prof.get("exchangeShortName"),
        "country":   prof.get("country"),
        "sector":    prof.get("sector"),
        "industry":  prof.get("industry"),
        "marketCap": mcap,
        "EV":        ev,
        "EBIT":      ebit,
        "NWC":       nwc,
        "PPE_Net":   ppe,
        "Capital":   capital,
        "Cash":      cash,
        "TotalDebt": debt,
        "EY":        ey,
        "ROC":       roc,
        "Goodwill":  goodwill,
        "Intangibles": intangibles,
    }

def _compute_z_score(prof: dict, inc: list, bal: list) -> Optional[float]:
    """Altman Z-score (public company version)."""
    try:
        b = bal[0] if bal else {}
        ta   = b.get("totalAssets") or 0
        tl   = b.get("totalLiabilities") or 0
        ca   = b.get("totalCurrentAssets") or 0
        cl   = b.get("totalCurrentLiabilities") or 0
        re   = b.get("retainedEarnings") or 0
        mcap = float(prof.get("mktCap") or prof.get("marketCap") or 0)
        ebit = _latest(inc, "operatingIncome") or 0
        rev  = _latest(inc, "revenue") or 0

        if ta <= 0 or tl <= 0:
            return None

        x1 = (ca - cl) / ta
        x2 = re / ta
        x3 = ebit / ta
        x4 = mcap / tl
        x5 = rev / ta
        return round(1.2*x1 + 1.4*x2 + 3.3*x3 + 0.6*x4 + 1.0*x5, 3)
    except Exception as e:
        print(f"[_compute_z_score] {type(e).__name__}: {e}", flush=True)
        return None


def _compute_f_score(inc: list, bal: list, cf: list) -> Optional[int]:
    """Piotroski F-score (0–9). Requires 2 periods of income+balance, 1 of cashflow."""
    try:
        if len(bal) < 2 or len(inc) < 2 or not cf:
            return None

        b0, b1 = bal[0], bal[1]   # current, prior
        i0, i1 = inc[0], inc[1]
        c0 = cf[0]

        ta0 = b0.get("totalAssets") or 0
        ta1 = b1.get("totalAssets") or 1
        if ta0 <= 0:
            return None

        # Profitability
        roa0 = (i0.get("netIncome") or 0) / ta0
        roa1 = (i1.get("netIncome") or 0) / ta1
        ocf  = c0.get("operatingCashFlow") or 0
        f1 = int(roa0 > 0)
        f2 = int(ocf > 0)
        f3 = int(roa0 > roa1)
        f4 = int((ocf / ta0) > roa0)

        # Leverage / liquidity / dilution
        ltd0 = (b0.get("longTermDebt") or 0) / ta0
        ltd1 = (b1.get("longTermDebt") or 0) / ta1
        cr0  = (b0.get("totalCurrentAssets") or 0) / max(b0.get("totalCurrentLiabilities") or 1, 1)
        cr1  = (b1.get("totalCurrentAssets") or 0) / max(b1.get("totalCurrentLiabilities") or 1, 1)
        sh0  = b0.get("commonStock") or b0.get("sharesOutstanding") or 0
        sh1  = b1.get("commonStock") or b1.get("sharesOutstanding") or 0
        f5 = int(ltd0 < ltd1)
        f6 = int(cr0 > cr1)
        f7 = int(sh0 <= sh1)

        # Operating efficiency
        rev0  = i0.get("revenue") or 0
        rev1  = i1.get("revenue") or 1
        cogs0 = i0.get("costOfRevenue") or 0
        cogs1 = i1.get("costOfRevenue") or 0
        gm0 = (rev0 - cogs0) / rev0 if rev0 else 0
        gm1 = (rev1 - cogs1) / rev1 if rev1 else 0
        at0 = rev0 / ta0 if ta0 else 0
        at1 = rev1 / ta1 if ta1 else 0
        f8 = int(gm0 > gm1)
        f9 = int(at0 > at1)

        return f1+f2+f3+f4+f5+f6+f7+f8+f9
    except Exception as e:
        print(f"[_compute_f_score] {type(e).__name__}: {e}", flush=True)
        return None    

#---------------------compute from vault----------------
def compute_mf_from_vault(symbol: str, vault: dict, annual: bool = True) -> Optional[Dict[str, Any]]:
    """
    Compute MF metrics from pre-fetched vault blobs.
    vault = {"profile": [...], "income-statement": [...], "balance-sheet-statement": [...], ...}
    """
    try:
        prof = vault.get("profile")
        if isinstance(prof, list):
            prof = prof[0] if prof else None
        if not prof:
            return None


        inc = vault.get("income-statement", [])
        bal_raw = vault.get("balance-sheet-statement", [])
        bal = bal_raw if isinstance(bal_raw, list) else [bal_raw]

        result = _compute_mf_metrics(prof, inc, bal, annual)

        if result is None or "reason" in result:
            return None

        return {"ticker": symbol, **result}

    except Exception as e:
        print(f"[compute_mf_from_vault] {symbol}: {e}")
        return None

# -------------------- SQLite Cache --------------------

DB_PATH = Path(__file__).parent / "cache.db"

def get_conn() -> sqlite3.Connection:
    """Return a WAL-enabled connection with busy timeout and Row factory."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    """Initialize the SQLite database with the company cache table."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_cache (
            ticker TEXT NOT NULL,
            period TEXT NOT NULL,
            name TEXT,
            exchange TEXT,
            country TEXT,
            sector TEXT,
            industry TEXT,
            marketCap REAL,
            EV REAL,
            EBIT REAL,
            NWC REAL,
            PPE_Net REAL,
            Capital REAL,
            Cash REAL,
            TotalDebt REAL,
            EY REAL,
            ROC REAL,
            Goodwill REAL,
            Intangibles REAL,
            ZScore REAL,
            FScore INTEGER,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (ticker, period)
        )
    """)
    conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_json_vault (
                ticker TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                json_blob TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (ticker, endpoint)
            )
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_control (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO scan_control (key, value)
        VALUES ('deepscan_paused', '0')
    """)
    conn.commit()
    existing = {row[1] for row in conn.execute("PRAGMA table_info(company_cache)")}
    for col, typedef in [("ZScore", "REAL"), ("FScore", "INTEGER")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE company_cache ADD COLUMN {col} {typedef}")
    conn.commit()
    conn.close()

# Initialize database on module load
_init_db()
# -------------------- Company Metrics --------------------
def fetch_blob(symbol, endpoint):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT json_blob FROM raw_json_vault WHERE ticker = ? AND endpoint = ?",
            (symbol, endpoint)
        ).fetchone()
    finally:
        conn.close()
        
    if row is None:
        return None
    return json.loads(row[0])

def fetch_vault_tickers():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM raw_json_vault ",            
        ).fetchall()
    finally:
        conn.close()        

    return [row[0] for row in rows]

def fetch_company(symbol, include_goodwill=False, include_intangibles=False,
                  compute_z=False, compute_f=False):

    try:
        prof = fetch_blob(symbol, "profile")
        inc  = fetch_blob(symbol, "income-statement")
        bal  = fetch_blob(symbol, "balance-sheet-statement")

        if not prof or not inc or not bal:
            return {"type": "skip", "ticker": symbol, "name": symbol, "reason": "Not in vault"}

        prof = prof[0]
        result = _compute_mf_metrics(prof, inc, bal, False, include_goodwill, include_intangibles)

        if result is None:
            return None
        if "reason" in result:
            return {"type": "skip", "ticker": symbol, "name": prof.get("companyName", symbol),
                    "reason": result["reason"]}

        if compute_z:
            result["ZScore"] = _compute_z_score(prof, inc, bal)
        if compute_f:
            cf = fetch_blob(symbol, "cash-flow-statement")
            result["FScore"] = _compute_f_score(inc, bal, cf) if cf else None

        return {"type": "success", "ticker": symbol, **result}

    except Exception as e:
        return {"type": "skip", "ticker": symbol, "name": symbol, "reason": f"Exception: {e}"}
# -------------------- Ranking --------------------

def magic_formula_rank(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["EY", "ROC"]).copy()
    df["EY_rank"] = df["EY"].rank(ascending=False, method="min")
    roc_for_rank = df["ROC"].clip(upper=ROC_RANK_CAP) if ROC_RANK_CAP else df["ROC"]
    df["ROC_rank"] = roc_for_rank.rank(ascending=False, method="min")
    df["MF_score"] = df["EY_rank"] + df["ROC_rank"]
    return df.sort_values(["MF_score", "EY_rank", "ROC_rank"]) 

# -------------------- CLI --------------------

def main():
    start_time = time.time()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    default_name = f"magic_formula_{timestamp}.csv"

    ap = argparse.ArgumentParser(description="Magic Formula screener (FMP)")

    ap.add_argument("--ex", "--exchanges", dest="exchanges", type=str, default="NASDAQ,NYSE,AMEX",
                    help="Comma‑separated FMP exchange codes (e.g., NASDAQ,NYSE,LSE)")
    ap.add_argument("--min-mcap", type=float, default=50e6, help="Minimum market cap USD")
    ap.add_argument("--limit", type=int, default=400, help="Max symbols to process (free‑tier friendly); web UI defaults to 4000 to cover the full NASDAQ+NYSE+AMEX universe")
    ap.add_argument("--sleep", type=float, default=0.2, help="Delay between API calls to respect rate limits")
    ap.add_argument("--top", type=int, default=30, help="How many top results to export")
    ap.add_argument("--no-random", action="store_true", default=False, help="Disable symbol shuffling (default: shuffle, matching web UI)")
    ap.add_argument("--countries", type=str, default=None,
                help="Comma-separated country codes (e.g., US,CA,GB). Default: US")
    ap.add_argument("--tier1", action="store_true",
                help="Use Tier 1 markets: US, SG, GB, CA")
    ap.add_argument("--out", type=str, default=default_name, help="Output CSV path")
    ap.add_argument("--annual", action="store_true",
                help="Use annual data instead of TTM quarterly")
    ap.add_argument("--check-debt-revenue", action="store_true",
                    help="Health check: require D/E trending down and revenue trending up")
    ap.add_argument("--check-cashflow", action="store_true",
                    help="Health check: require OCF > net income for consecutive quarters")
    ap.add_argument("--health-checks", action="store_true",
                    help="Shortcut: enable both --check-debt-revenue and --check-cashflow")
    ap.add_argument("--debt-revenue-quarters", type=int, default=6,
                    help="Quarters of history for D/E + revenue trend check (default: 6)")
    ap.add_argument("--cashflow-quarters", type=int, default=8,
                    help="Quarters of history for OCF > net income check (default: 8)")
    ap.add_argument("--goodwill", action="store_true", dest="include_goodwill",
                    help="Include goodwill in capital calculation")
    ap.add_argument("--intangibles", action="store_true", dest="include_intangibles",
                    help="Include intangibles in capital calculation")
    ap.add_argument("--zscore", action="store_true", help="Compute and output Altman Z-score")
    ap.add_argument("--fscore", action="store_true", help="Compute and output Piotroski F-score")

    args = ap.parse_args()

 # Parse countries
    if args.tier1:
        countries = ["US", "SG", "GB", "CA"]
    elif args.countries:
        countries = [c.strip() for c in args.countries.split(',')]
    else:
        countries = ["US"]  # matches web UI default; use --countries or --tier1 to override

 
    exchanges = [x.strip() for x in args.exchanges.split(',') if x.strip()]

    # Pull company data
    symbols = fetch_vault_tickers()

    records = []
    skipped = []
    for sym in tqdm(symbols, desc="Pulling fundamentals"):
        rec = fetch_company(sym, args.include_goodwill, args.include_intangibles,
                            args.zscore, args.fscore)
        if rec:
            if rec.get("type") == "success" and rec.get("marketCap", 0) >= args.min_mcap:
                records.append(rec)
            elif rec.get("type") == "skip":
                skipped.append({
                    "ticker": rec.get("ticker"),
                    "name": rec.get("name", ""),
                    "reason": rec.get("reason", "Unknown")
                })

    if not records:
        print("No qualifying records. Is the vault populated? Try lowering --min-mcap.")
        return

    df = pd.DataFrame(records)
    ranked = magic_formula_rank(df)

    # Apply health checks only to top candidates
    check_debt_revenue = args.check_debt_revenue or args.health_checks
    check_cashflow = args.check_cashflow or args.health_checks
    if check_debt_revenue or check_cashflow:
        top_candidates = ranked.head(args.top)
        healthy_tickers = []

        for ticker in tqdm(top_candidates["ticker"], desc="Running health checks"):
            health = check_financial_health(
                ticker,
                check_debt_revenue=check_debt_revenue,
                check_cashflow_quality=check_cashflow,
                debt_revenue_quarters=args.debt_revenue_quarters,
                cashflow_quarters=args.cashflow_quarters,
            )
            if health["passes_all"]:
                healthy_tickers.append(ticker)

        ranked = ranked[ranked["ticker"].isin(healthy_tickers)]
        print(f"Health checks: {len(healthy_tickers)}/{len(top_candidates)} passed")

    cols = [
        "ticker","name","exchange","country","sector","industry",
        "marketCap","EV","EBIT","NWC","PPE_Net","Capital","Cash","TotalDebt",
        "EY","ROC","EY_rank","ROC_rank","MF_score","Goodwill","Intangibles",
        "ZScore", "FScore"
    ]
    cols = [c for c in cols if c in ranked.columns]

    out = ranked[cols].head(args.top)
 
    """
    out.to_csv(args.out, index=False)
    
# Save skipped stocks to separate CSV
    if skipped:
        skipped_file = args.out.replace(".csv", "_skipped.csv")
        skipped_df = pd.DataFrame(skipped)
        skipped_df.to_csv(skipped_file, index=False)
        print(f"\nSkipped {len(skipped)} stocks (saved to: {skipped_file})")

    print("Top results saved to:", args.out)
    """
 
    print(out.to_string(index=False, max_colwidth=28))
 
    elapsed = time.time() - start_time
    minutes, seconds = divmod(int(elapsed), 60)
    print(f"Completed in {minutes}m {seconds}s")

 
if __name__ == "__main__": 
    main()
