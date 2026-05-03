#!/usr/bin/env python3
"""
Check NWC-related balance sheet fields from FMP for one or more tickers.
Usage: python check_nwc.py HPQ AAPL CRCT
"""
import os, sys, requests

FMP_BASE = "https://financialmodelingprep.com/api/v3"
API_KEY = os.getenv("FMP_API_KEY", "")

if not API_KEY:
    print("ERROR: FMP_API_KEY not set")
    sys.exit(1)

tickers = sys.argv[1:] if len(sys.argv) > 1 else ["HPQ"]

def fmp_get(path, params=None):
    p = dict(params or {})
    p["apikey"] = API_KEY
    r = requests.get(f"{FMP_BASE}{path}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()

NWC_FIELDS = [
    # Aggregates
    "totalCurrentAssets",
    "totalCurrentLiabilities",
    "cashAndShortTermInvestments",
    "cashAndCashEquivalents",
    "totalDebt",
    "shortTermDebt",
    # Operating current assets
    "netReceivables",
    "inventory",
    "otherCurrentAssets",
    # Operating current liabilities
    "accountPayables",
    "otherCurrentLiabilities",
    "taxPayables",
    "deferredRevenue",
]

for ticker in tickers:
    bal = fmp_get(f"/balance-sheet-statement/{ticker}", {"period": "quarter", "limit": 1})
    if not bal:
        print(f"\nNo data for {ticker}")
        continue

    row = bal[0]
    print(f"\n{'='*60}")
    print(f"{ticker} — {row.get('date')} ({row.get('period')})")
    print(f"{'='*60}")
    print(f"{'Field':<35} {'Value':>20}")
    print("-" * 57)

    for f in NWC_FIELDS:
        v = row.get(f)
        print(f"{f:<35} {str(v):>20}")

    # Calculate NWC three ways
    tca = row.get("totalCurrentAssets") or 0
    tcl = row.get("totalCurrentLiabilities") or 0
    cash = row.get("cashAndShortTermInvestments") or 0
    debt = row.get("shortTermDebt") or row.get("totalDebt") or 0
    receivables = row.get("netReceivables") or 0
    inventory = row.get("inventory") or 0
    other_ca = row.get("otherCurrentAssets") or 0
    payables = row.get("accountPayables") or 0
    other_cl = row.get("otherCurrentLiabilities") or 0

    nwc_standard = tca - tcl
    nwc_modified = (tca - cash) - (tcl - debt)
    nwc_operating = (receivables + inventory + other_ca) - (payables + other_cl)

    print(f"\n{'NWC Calculations':}")
    print(f"  Standard (TCA - TCL):                  {nwc_standard:>20,.0f}")
    print(f"  Modified (TCA-cash) - (TCL-debt):      {nwc_modified:>20,.0f}")
    print(f"  Operating (OCA - OCL):                 {nwc_operating:>20,.0f}")
    print(f"  Difference modified vs operating:      {nwc_modified - nwc_operating:>20,.0f}")
