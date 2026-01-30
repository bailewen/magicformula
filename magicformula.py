#!/usr/bin/env python3
"""
Magic Formula (Joel Greenblatt) — FMB
 edition
-------------------------------------------------
This code uses Financial Modeling Prep (FMP).
Starter Annual Plan

Requirements (Manjaro/Arch):
  python -m venv venv && source venv/bin/activate
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
 
 2.0 cleaned up referrences to Finnhub & adapted rate limits to FMP paid tier, added parameters to fmp_get() to filter for only actual stocks
2.2 switched to TTM and added debugging visibility (error messages)

"""
from __future__ import annotations
import os, time, argparse, math
from typing import Dict, Any, List, Optional
import requests
import pandas as pd
import numpy as np
import json
import datetime
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import deque
from datetime import timedelta

#---filter dataset for actual active stock 


#---rate limiter
class RateLimiter:
    """Simple per-minute rate limiter."""
    def __init__(self, calls_per_minute=300):
        self.calls_per_minute = calls_per_minute
        self.window = 60.0
        self.calls = deque()

    def wait(self):
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
    def tqdm(x, **k):
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

def fmp_get(path: str, params: Optional[Dict[str, Any]] = None, retries: int = 3, backoff: float = 0.8):
    if params is None:
        params = {}

    api_key = os.getenv("FMP_API_KEY", "")
    if not api_key:
        raise RuntimeError("FMP_API_KEY not set. export FMP_API_KEY=YOUR_KEY")
 
    params = dict(params)
    params["apikey"] = api_key
    url = f"{FMP_BASE}{path}"
    for a in range(retries):
        limiter.wait()  # reuse your existing RateLimiter
        r = S.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(backoff * (a + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise requests.HTTPError(f"Failed after retries: {url}")


# -------------------- Data pulling --------------------

def list_symbols(ex: str, min_mcap: float = 50e6, countries: List[str] = None) -> List[Dict[str, Any]]:
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
    ) or []
    
    filtered = []
    for r in rows:
        sym = r.get("symbol")
        mcap = r.get("marketCap") or 0

        # --- filter out ETFs, funds, warrants, preferreds, SPACs, microcaps ---
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

def fmp_profile(ticker: str) -> Dict[str, Any]:
    prof = fmp_get(f"/profile/{ticker}") or []
    return prof[0] if isinstance(prof, list) and prof else {}


def fmp_income(ticker: str, annual: bool = False) -> List[Dict[str, Any]]:
    if annual:
        return fmp_get(f"/income-statement/{ticker}", {"period": "annual", "limit": 1}) or []
    return fmp_get(f"/income-statement/{ticker}", {"period": "quarter", "limit": 4}) or []

def fmp_balance(ticker: str) -> List[Dict[str, Any]]:
    return fmp_get(f"/balance-sheet-statement/{ticker}", {"period": "quarter", "limit": 1}) or []

def check_financial_health(symbol: str, 
    check_debt_revenue: bool = False,
    check_cashflow_quality: bool = False,
    debt_revenue_quarters: int = 6,
    cashflow_quarters: int = 8) -> dict:
    """
    Optional health checks.
    Returns dict with pass/fail for each enabled check.
    """
    results = {"symbol": symbol, "passes_all": True}
    
    # Check 1: D/E decreasing while revenue increasing
    if check_debt_revenue:
        try:
            bs_data = fmp_get(f"/balance-sheet-statement/{symbol}", 
                             {"period": "quarter", "limit": debt_revenue_quarters})
            is_data = fmp_get(f"/income-statement/{symbol}", 
                             {"period": "quarter", "limit": debt_revenue_quarters})
            
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
        except Exception:
            results["debt_revenue_check"] = None
        
        if results.get("debt_revenue_check") is False:
            results["passes_all"] = False
    
    # Check 2: OCF > Net Income for consecutive quarters
    if check_cashflow_quality:
        try:
            cf_data = fmp_get(f"/cash-flow-statement/{symbol}", 
                             {"period": "quarter", "limit": cashflow_quarters})
            
            if cf_data and len(cf_data) >= 4:
                ocf_beats_ni = all(
                    (q.get("operatingCashFlow") or 0) > (q.get("netIncome") or 0)
                    for q in cf_data
                )
                results["cashflow_quality_check"] = ocf_beats_ni
            else:
                results["cashflow_quality_check"] = None
        except Exception:
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


# -------------------- Field helpers --------------------

def pull_company(symbol: str, annual: bool = False) -> Optional[Dict[str, Any]]:
 
    try:
        prof = fmp_profile(symbol)
        if not prof:
            return None

        # Country filter handled in list_symbols
     
        # Exclude sectors
        if prof.get("sector") in EXCLUDE_SECTORS:
            return None

        inc = fmp_income(symbol, annual)
        bal = fmp_balance(symbol)

        # EBIT proxy (FMP uses operatingIncome)
        #ebit = _latest(inc, "operatingIncome")
        #ebit = sum(item.get("operatingIncome") or 0 for item in inc) if inc else None #annual reports

        # TTM EBIT: sum last 4 quarters
        if inc and len(inc) >= 4:
            ebit = sum(q.get("operatingIncome") or 0 for q in inc[:4])
        elif inc:
            # Fallback: annualize if fewer than 4 quarters available
            ebit = sum(q.get("operatingIncome") or 0 for q in inc) * (4 / len(inc))
        else:
            ebit = None
         
        # EBIT calculation
        if annual:
            ebit = _latest(inc, "operatingIncome")
        elif inc and len(inc) >= 4:
            ebit = sum(q.get("operatingIncome") or 0 for q in inc[:4])
        elif inc:
            ebit = sum(q.get("operatingIncome") or 0 for q in inc) * (4 / len(inc))
        else:
            ebit = None
     

        # Balance sheet fields
        tca  = _latest(bal, "totalCurrentAssets")
        tcl  = _latest(bal, "totalCurrentLiabilities")
        ppe  = _latest(bal, "propertyPlantEquipmentNet")
        cash = _latest(bal, "cashAndShortTermInvestments")
        debt = _first_available(bal, ["totalDebt", "shortTermDebt", "longTermDebt"])

        # Market cap from profile (FMP 'mktCap' sometimes named 'marketCap')
        mcap = prof.get("mktCap") if prof.get("mktCap") is not None else prof.get("marketCap")
        try:
            mcap = float(mcap) if mcap is not None else None
        except Exception:
            mcap = None

        #if None in (ebit, tca, tcl, ppe, cash, debt, mcap):
        #    return None
        # --- check for why nothing is being found ---
        if None in (ebit, tca, tcl, ppe, cash, debt, mcap):
            missing = []
            if ebit is None: missing.append("ebit")
            if tca is None: missing.append("tca")
            if tcl is None: missing.append("tcl")
            if ppe is None: missing.append("ppe")
            if cash is None: missing.append("cash")
            if debt is None: missing.append("debt")
            if mcap is None: missing.append("mcap")

            # This will tell us exactly why the TTM endpoint is failing
            print(f"DEBUG: Skipping {symbol} -> Missing: {missing}")
            return None



        ev = mcap + debt - cash
        """# The Gemini suggestion to deal with negative capital
        # nwc = tca - tcl #working capital = total current assets - total current liabilities
        nwc = (tca - cash) - (tcl - debt) #don't count cash on hand as an asset, and subtract debt from capital
        capital = nwc + ppe
        
        if ev <= 0:
            return None
        capital = max(capital, 1)

        ey = ebit / ev
        roc = ebit / capital
        """

        # The Claude suggestion
        nwc = (tca - cash) - (tcl - debt)
        nwc = max(nwc, 0)  # Floor NWC at zero, not the whole capital
        capital = nwc + ppe

        if ev <= 0:
            return None
        if capital <= 0:  # Only skip if PPE is also zero/negative (data issue)
            return None

        ey = ebit / ev
        roc = ebit / capital

        # Sanity filters
        if roc > 1.0:  # Cap ROC at 100%
            roc = 1.0
        if ey > 0.5:  # Flag: EY > 50% is usually data error
            return None
        if capital < 10e6:  # Require minimum $10M capital base
            return None

        return {
            "ticker": symbol,
            "name": prof.get("companyName") or prof.get("company") or prof.get("symbol"),
            "exchange": prof.get("exchangeShortName"),
            "country": prof.get("country"),
            "sector": prof.get("sector"),
            "industry": prof.get("industry"),
            "marketCap": mcap,
            "EV": ev,
            "EBIT": ebit,
            "NWC": nwc,
            "PPE_Net": ppe,
            "Capital": capital,
            "Cash": cash,
            "TotalDebt": debt,
            "EY": ey,
            "ROC": roc,
        }
    except Exception:
        return None


CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

def pull_company_cached(symbol, annual=False):
    cache_file = CACHE_DIR / f"{symbol}.json"
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < timedelta(days=7).total_seconds():
            return json.loads(cache_file.read_text())
    rec = pull_company(symbol, annual)
    if rec:
        cache_file.write_text(json.dumps(rec))
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
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    default_name = f"magic_formula_{timestamp}.csv"

    ap = argparse.ArgumentParser(description="Magic Formula screener (FMP)")

    ap.add_argument("--ex", "--exchanges", dest="exchanges", type=str, default="NASDAQ,NYSE,AMEX",
                    help="Comma‑separated FMP exchange codes (e.g., NASDAQ,NYSE,LSE)")
    ap.add_argument("--min-mcap", type=float, default=50e6, help="Minimum market cap USD")
    ap.add_argument("--limit", type=int, default=400, help="Max symbols to process (free‑tier friendly)")
    ap.add_argument("--sleep", type=float, default=0.2, help="Delay between API calls to respect rate limits")
    ap.add_argument("--top", type=int, default=30, help="How many top results to export")
    ap.add_argument("--random", action="store_true", help="Shuffle symbols before limiting (for random sampling)")
    ap.add_argument("--countries", type=str, default=None,
                help="Comma-separated country codes (e.g., US,CA,GB). Default: US")
    ap.add_argument("--tier1", action="store_true",
                help="Use Tier 1 markets: US, SG, GB, CA")
    ap.add_argument("--out", type=str, default=default_name, help="Output CSV path")
    ap.add_argument("--annual", action="store_true",
                help="Use annual data instead of TTM quarterly")

    args = ap.parse_args()

 # Parse countries
    if args.tier1:
        countries = ["US", "SG", "GB", "CA"]
    elif args.countries:
        countries = [c.strip() for c in args.countries.split(',')]
    else:
        countries = ["US"]  # Default to US only

 
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

    if args.random:
        print(f"Randomizing {len(symbols)} symbols before sampling...")
        random.shuffle(symbols)

    if args.limit and len(symbols) > args.limit:
        symbols = symbols[:args.limit]
    
    # Pull company data

    records = []
    for sym in tqdm(symbols, desc="Pulling fundamentals"):
        rec = pull_company_cached(sym, args.annual)
        if rec and rec.get("marketCap", 0) >= args.min_mcap:
            records.append(rec)

    if not records:
        print("No qualifying records. Try increasing --limit or lowering --min-mcap.")
        return

    df = pd.DataFrame(records)
    ranked = magic_formula_rank(df)

    cols = [
        "ticker","name","exchange","country","sector","industry",
        "marketCap","EV","EBIT","NWC","PPE_Net","Capital","Cash","TotalDebt",
        "EY","ROC","EY_rank","ROC_rank","MF_score"
    ]
    cols = [c for c in cols if c in ranked.columns]

    out = ranked[cols].head(args.top)
    out.to_csv(args.out, index=False)

#notification when script is done 
    try:
        import subprocess
        subprocess.run([
            "notify-send",
            "Magic Formula Scan Complete",
            f"Successfully processed {args.limit} stocks.\nTop {args.top} results saved to {args.out}.",
            "--icon=utilities-terminal"
        ])
    except Exception as e:
        pass  # Silently fail if notify-send is missing

    print("Top results saved to:", args.out)
    print(out.to_string(index=False, max_colwidth=28))

if __name__ == "__main__":
    main()
