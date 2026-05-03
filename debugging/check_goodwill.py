#!/usr/bin/env python3
"""
Quick check: does FMP return goodwill/intangibles for a given ticker?
Usage: python check_goodwill.py HPQ
"""
import os, sys, requests

FMP_BASE = "https://financialmodelingprep.com/api/v3"
API_KEY = os.getenv("FMP_API_KEY", "")

if not API_KEY:
    print("ERROR: FMP_API_KEY not set")
    sys.exit(1)

ticker = sys.argv[1] if len(sys.argv) > 1 else "HPQ"

def fmp_get(path, params=None):
    p = dict(params or {})
    p["apikey"] = API_KEY
    r = requests.get(f"{FMP_BASE}{path}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()

bal = fmp_get(f"/balance-sheet-statement/{ticker}", {"period": "quarter", "limit": 1})

if not bal:
    print(f"No balance sheet data for {ticker}")
    sys.exit(1)

row = bal[0]

fields = [
    "goodwill",
    "intangibleAssets",
    "goodwillAndIntangibleAssets",
    "propertyPlantEquipmentNet",
    "totalAssets",
    "totalCurrentAssets",
    "totalCurrentLiabilities",
    "cashAndShortTermInvestments",
    "totalDebt",
]

print(f"\n{ticker} balance sheet fields:")
print(f"{'Field':<35} {'Value':>20}")
print("-" * 57)
for f in fields:
    v = row.get(f)
    print(f"{f:<35} {str(v):>20}")

print(f"\nAll available fields in response:")
for k, v in sorted(row.items()):
    if v not in (None, "", 0):
        print(f"  {k:<40} {v}")
