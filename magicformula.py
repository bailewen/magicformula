#!/usr/bin/env python3
"""
Magic Formula (Joel Greenblatt) — FMB
 edition
-------------------------------------------------
This code uses Financial Modeling Prep (FMP).
Starter Annual Plan

Requirements (Manjaro/Arch):
  python -m venv && source venv/bin/activate
  pip install requests pandas numpy tqdm

Environment:
  export FMP_API_KEY="YOUR_KEY"

Notes:
- Symbols are pulled per‑exchange via `/stock/symbol?exchange=...`.
- Financials are pulled via standardized `/financials` (income & balance sheet).
- Profile (market cap, country, sector) via `/stock/profile2`.
- EY = EBIT / EV, where EV = marketCap + totalDebt - cashAndCashEquivalents
- ROC = EBIT / (NWC + Net Fixed Assets)
    NWC = totalCurrentAssets - totalCurrentLiabilities
    Net Fixed = propertyPlantEquipmentNet
- Excludes financials & utilities.

Caveats:
- Field names from FMP standardized statements can differ slightly
  by market/time. Fallback helpers are included.

Run:
 python magicformula.2.0.py --ex NASDAQ,NYSE,AMEX --top 30 --min-mcap 5e7 --limit 400
 
2.0 cleaned up references to Finnhub & adapted rate limits to FMP paid tier, added parameters to fmp_get() to filter for only actual stocks
2.2 switched to TTM and added debugging visibility (error messages)

*********CHANGE LIST******************
2026-01-31: 
- stopped numbering file name with version. Updating aliases was annoying
- added parallel processing (ThreadPoolExecutor)
- added -ttm flag (default is annual)
- default countries = none (US markets only, but intl companies still included)
- Run should report how long it took now
2026-02-01



"""
from __future__ import annotations
import os, time, argparse
from typing import Dict, Any, List, Optional
import requests
import pandas as pd
import datetime
import random
import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import deque
import threading

#---filter dataset for actual active stock 


#---rate limiter
class RateLimiter:
    """Simple per-minute rate limiter."""
    def __init__(self, calls_per_minute=300):
        self.calls_per_minute = calls_per_minute
        self.window = 60.0
        self.calls = deque()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            # pop timestamps older than 60s
            while self.calls and now - self.calls[0] > self.window:
                self.calls.popleft()
            if len(self.calls) >= self.calls_per_minute:
                sleep_for = self.window - (now - self.calls[0]) + 0.01
                if sleep_for > 0:
                    time.sleep(sleep_for)
            self.calls.append(time.time())
limiter = RateLimiter(calls_per_minute=300)

try:
    from tqdm import tqdm  # progress bar (optional)
except Exception:  # pragma: no cover
    def tqdm(x, **_):
        return x

FMP_BASE = "https://financialmodelingprep.com/api/v3"
FMP_KEY  = os.getenv("FMP_API_KEY", "")

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
S.headers.update({"User-Agent": "MagicFormulaFMB/1.0"})

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
        limiter.wait()  # reuse your existing RateLimiter
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

def fmp_profile(ticker: str, api_key: str = None) -> Dict[str, Any]:
    prof = fmp_get(f"/profile/{ticker}", api_key=api_key) or []
    return prof[0] if isinstance(prof, list) and prof else {}


def fmp_income(ticker: str, annual: bool = False, api_key: str = None) -> List[Dict[str, Any]]:
    if annual:
        return fmp_get(f"/income-statement/{ticker}", {"period": "annual", "limit": 2}, api_key=api_key) or []
    return fmp_get(f"/income-statement/{ticker}", {"period": "quarter", "limit": 4}, api_key=api_key) or []

def fmp_balance(ticker: str, api_key: str = None) -> List[Dict[str, Any]]:
    return fmp_get(f"/balance-sheet-statement/{ticker}", {"period": "quarter", "limit": 2}, api_key=api_key) or []

def fmp_cashflow(ticker: str, annual: bool = False, api_key: str = None) -> List[Dict[str, Any]]:
    limit = 2 if annual else 8
    period = "annual" if annual else "quarter"
    return fmp_get(f"/cash-flow-statement/{ticker}", {"period": period, "limit": limit}, api_key=api_key) or []

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

def _first_available(items: List[Dict[str, Any]], fields: List[str]):
    if not items:
        return None
    for f in fields:
        v = items[0].get(f)
        if v not in (None, ""):
            try:
                return float(v)
            except Exception:
                return None
    return None

def _sum_ttm(items: List[Dict[str, Any]], field: str, start_idx: int = 0) -> float:
    try:
        return sum(float(q.get(field) or 0) for q in items[start_idx:start_idx+4])
    except Exception:
        return 0.0

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

    name = prof.get("companyName", "").lower()
    if any(x in name for x in ["preferred", "perpetual", "series a", "series b"]):
        return None

    if annual:
        ebit = _latest(inc, "operatingIncome")
    elif inc and len(inc) >= 4:
        ebit = sum(q.get("operatingIncome") or 0 for q in inc[:4])
    else:
        ebit = None

    tca  = _latest(bal, "totalCurrentAssets")
    tcl  = _latest(bal, "totalCurrentLiabilities")
    ppe  = _latest(bal, "propertyPlantEquipmentNet")
    cash = _latest(bal, "cashAndShortTermInvestments")
    debt = _first_available(bal, ["totalDebt", "shortTermDebt", "longTermDebt"]) or 0.0

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

    nwc = (tca - cash) - (tcl - debt)
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
    if capital <= 0:
        return {"reason": "Negative or zero capital"}
    if capital < 10e6:
        return {"reason": "Capital < $10M"}
    if ebit < 0:
        return {"reason": "Negative EBIT"}

    ey  = ebit / ev
    roc = ebit / capital

    if roc > 10.0:
        return {"reason": "ROC > 1000% (data error)"}

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

def pull_company(symbol: str, annual: bool = False, include_goodwill: bool = False,
                 include_intangibles: bool = False, compute_z: bool = False,
                 compute_f: bool = False, api_key: str = None) -> Optional[Dict[str, Any]]:
    try:
        prof = fmp_profile(symbol, api_key=api_key)
        if not prof:
            return {"type": "skip", "ticker": symbol, "name": symbol, "reason": "No profile data"}

        inc = fmp_income(symbol, annual, api_key=api_key)
        bal = fmp_balance(symbol, api_key=api_key)

        result = _compute_mf_metrics(prof, inc, bal, annual, include_goodwill, include_intangibles)

        if result and "reason" not in result:
            if compute_z:
                result["ZScore"] = _compute_z_score(prof, inc, bal)
        #        if result["ZScore"] is not None and result["ZScore"] < 2.99:
        #            return {"type": "skip", "ticker": symbol, "name": prof.get("companyName", symbol),
        #                    "reason": f"Z-score {result['ZScore']} below 2.99"}
            if compute_f:
                cf = None
                conn = get_conn()
                row = conn.execute(
                    "SELECT json_blob FROM raw_json_vault WHERE ticker = ? AND endpoint = 'cash-flow-statement'",
                    (symbol,)
                ).fetchone()
                conn.close()
                if row:
                    cf = json.loads(row[0])
                else:
                    cf = fmp_cashflow(symbol, annual, api_key=api_key)
                result["FScore"] = _compute_f_score(inc, bal, cf)
        #       if result["FScore"] is None or result["FScore"] < 3:
        #            return {"type": "skip", "ticker": symbol, "name": prof.get("companyName", symbol),
        #                    "reason": f"F-score {result['FScore']} below 3"}

        if result is None:
            return None
        if "reason" in result:
            print(f"DEBUG: Skipping {symbol} -> {result['reason']}")
            return {"type": "skip", "ticker": symbol, "name": prof.get("companyName", symbol),
                    "reason": result["reason"]}

        return {"type": "success", "ticker": symbol, **result}

    except Exception as e:
        return {"type": "skip", "ticker": symbol, "name": symbol, "reason": f"Exception: {str(e)}"}

#---------------------compute from vault----------------
def compute_mf_from_vault(symbol: str, vault: dict, annual: bool = True) -> Optional[Dict[str, Any]]:
    """
    Compute MF metrics from pre-fetched vault blobs.
    vault = {"profile": [...], "income-statement": [...], "balance-sheet-statement": [...], ...}
    """
    try:
        prof_raw = vault.get("profile", [])
        prof = prof_raw[0] if isinstance(prof_raw, list) else prof_raw
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

def db_upsert(record: Dict[str, Any], period: str) -> None:
    """Insert or update a company record in the database."""
    if not record or record.get("type") != "success":
        return

    conn = get_conn()
    conn.execute("""
        INSERT INTO company_cache (
            ticker, period, name, exchange, country, sector, industry,
            marketCap, EV, EBIT, NWC, PPE_Net, Capital, Cash, TotalDebt, EY, ROC, Goodwill, Intangibles,
            ZScore, FScore,
            last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(ticker, period) DO UPDATE SET
            name = excluded.name,
            exchange = excluded.exchange,
            country = excluded.country,
            sector = excluded.sector,
            industry = excluded.industry,
            marketCap = excluded.marketCap,
            EV = excluded.EV,
            EBIT = excluded.EBIT,
            NWC = excluded.NWC,
            PPE_Net = excluded.PPE_Net,
            Capital = excluded.Capital,
            Cash = excluded.Cash,
            TotalDebt = excluded.TotalDebt,
            EY = excluded.EY,
            ROC = excluded.ROC,
            Goodwill = excluded.Goodwill,
            Intangibles = excluded.Intangibles,
            ZScore = excluded.ZScore,
            FScore = excluded.FScore,
            last_updated = CURRENT_TIMESTAMP
    """, (
        record.get("ticker"),
        period,
        record.get("name"),
        record.get("exchange"),
        record.get("country"),
        record.get("sector"),
        record.get("industry"),
        record.get("marketCap"),
        record.get("EV"),
        record.get("EBIT"),
        record.get("NWC"),
        record.get("PPE_Net"),
        record.get("Capital"),
        record.get("Cash"),
        record.get("TotalDebt"),
        record.get("EY"),
        record.get("ROC"),
        record.get("Goodwill"),
        record.get("Intangibles"),
        record.get("ZScore"),
        record.get("FScore"),
    ))
    conn.commit()
    conn.close()



def db_fetch(ticker: str, period: str, max_age_days: int = 7) -> Optional[Dict[str, Any]]:
    """Fetch a cached company record if it exists and is not expired."""
    conn = get_conn()
    cursor = conn.execute("""
        SELECT * FROM company_cache
        WHERE ticker = ? AND period = ?
        AND last_updated > datetime('now', ?)
    """, (ticker, period, f'-{max_age_days} days'))
    row = cursor.fetchone()
    conn.close()

    if row:
        return {
            "type": "success",
            "ticker": row["ticker"],
            "name": row["name"],
            "exchange": row["exchange"],
            "country": row["country"],
            "sector": row["sector"],
            "industry": row["industry"],
            "marketCap": row["marketCap"],
            "EV": row["EV"],
            "EBIT": row["EBIT"],
            "NWC": row["NWC"],
            "PPE_Net": row["PPE_Net"],
            "Capital": row["Capital"],
            "Cash": row["Cash"],
            "TotalDebt": row["TotalDebt"],
            "EY": row["EY"],
            "ROC": row["ROC"],
            "ZScore": row["ZScore"],
            "FScore": row["FScore"],
        }
    return None


def _set_deepscan_pause(paused: bool) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE scan_control SET value=? WHERE key='deepscan_paused'",
        ('1' if paused else '0',)
    )
    conn.commit()
    conn.close()


def fetch_company_with_cache(symbol: str, annual: bool = False, include_goodwill: bool = False,
                             include_intangibles: bool = False, compute_z: bool = False,
                             compute_f: bool = False, api_key: str = None) -> Optional[Dict[str, Any]]:

    period = "annual" if annual else "ttm"
    if include_goodwill:
        period += "_g"
    if include_intangibles:
        period += "_i"
    if compute_z:
        period += "_z"
    if compute_f:
        period += "_f"

    # Try to fetch from cache first
    cached = db_fetch(symbol, period)
    if cached:
        return cached
    rec = pull_company(symbol, annual, include_goodwill, include_intangibles,
                       compute_z=compute_z, compute_f=compute_f, api_key=api_key)
    # Cache successful records
    if rec and rec.get("type") == "success":
        db_upsert(rec, period)
    return rec

# -------------------- Ranking --------------------

def magic_formula_rank(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["EY", "ROC"]).copy()
    df["EY_rank"] = df["EY"].rank(ascending=False, method="min")
    df["ROC_rank"] = df["ROC"].rank(ascending=False, method="min")
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

    # Pull symbols per exchange
    symbols: List[str] = []
    for ex in exchanges:
        rows = list_symbols(ex, args.min_mcap, countries)
        for r in rows:
            sym = r.get("symbol") or r.get("displaySymbol")
            if sym:
                symbols.append(sym)
        time.sleep(args.sleep)

    # Dedup (filtering already done in list_symbols)
    symbols = list(dict.fromkeys(symbols))

    use_random = not args.no_random
    if use_random:
        print(f"Randomizing {len(symbols)} symbols before sampling...")
        random.shuffle(symbols)

    if args.limit and len(symbols) > args.limit:
        symbols = symbols[:args.limit]

    # Pull company data

    records = []
    skipped = []
    _set_deepscan_pause(True)
    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_company_with_cache, sym, args.annual, args.include_goodwill,
                                       args.include_intangibles, args.zscore, args.fscore): sym for sym in symbols}
            for future in tqdm(as_completed(futures), total=len(futures), desc="Pulling fundamentals"):
                try:
                    rec = future.result(timeout=5)
                    if rec:
                        if rec.get("type") == "success" and rec.get("marketCap", 0) >= args.min_mcap:
                            records.append(rec)
                        elif rec.get("type") == "skip":
                            skipped.append({
                                "ticker": rec.get("ticker"),
                                "name": rec.get("name", ""),
                                "reason": rec.get("reason", "Unknown")
                            })
                except TimeoutError:
                    print(f"[main] {futures[future]}: timeout (worker exceeded 5s)", flush=True)
                except Exception as e:
                    print(f"[main] {futures[future]}: {type(e).__name__}: {e}", flush=True)
    finally:
        _set_deepscan_pause(False)

    if not records:
        print("No qualifying records. Try increasing --limit or lowering --min-mcap.")
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
    out.to_csv(args.out, index=False)
    
# Save skipped stocks to separate CSV
    if skipped:
        skipped_file = args.out.replace(".csv", "_skipped.csv")
        skipped_df = pd.DataFrame(skipped)
        skipped_df.to_csv(skipped_file, index=False)
        print(f"\nSkipped {len(skipped)} stocks (saved to: {skipped_file})")

    print("Top results saved to:", args.out)
    print(out.to_string(index=False, max_colwidth=28))
 
    elapsed = time.time() - start_time
    minutes, seconds = divmod(int(elapsed), 60)
    print(f"Completed in {minutes}m {seconds}s")

 
if __name__ == "__main__": 
    main()
