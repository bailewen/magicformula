#!/usr/bin/env python3
"""
Compare ROC / capital definitions by how many of Greenblatt's official picks
each one lands in the top 30 and top 100 of the vault universe.

Reads raw_json_vault only (no API calls). Reuses the sanity checks and
exclusions from magicformula.py, then varies how capital (the ROC denominator)
is built and whether ROC is capped when ranking.

Usage (from the app directory, as the user that owns cache.db):
    venv/bin/python compare_definitions.py [--db cache.db] [--min-mcap 50000000] [--detail]
"""
import argparse
import json
import sqlite3

import pandas as pd

import magicformula as mf

# Greenblatt's official list as pasted on 2026-11-13 (update as needed).
OFFICIAL = {
    "AFMJF", "BCRX", "BMBL", "EGHSF", "HRB", "HRMY", "HPQ", "IRWD", "MGTX",
    "MNTN", "NRIX", "OMC", "OTEX", "PRDO", "PXED", "PBI", "PLTK", "QTTB",
    "RGS", "SNDK", "SBC", "SSTK", "SPRO", "STRA", "TP", "UPWK", "VXRT",
    "VSNT", "XPOF", "GTM",
}
# Foreign-listed names a US-only screen can't produce (my best guess).
FOREIGN = {"AFMJF", "EGHSF", "OTEX"}

VARIANTS = [
    ("baseline (current)",          {}),
    ("+ goodwill",                  {"gw": True}),
    ("+ intangibles",               {"intang": True}),
    ("+ goodwill & intangibles",    {"gw": True, "intang": True}),
    ("cash kept in NWC",            {"cash_in": True}),
    ("no NWC clamp",                {"clamp": False}),
    ("cash in + gw & intang",       {"cash_in": True, "gw": True, "intang": True}),
    ("assets method (TA-cash-opCL)", {"method": "assets"}),
]
CAPS = [None, 1.0]


def load_vault(db):
    conn = sqlite3.connect(db)
    data = {}
    q = ("SELECT ticker, endpoint, json_blob FROM raw_json_vault "
         "WHERE endpoint IN ('profile','income-statement','balance-sheet-statement')")
    for t, ep, blob in conn.execute(q):
        data.setdefault(t, {})[ep] = json.loads(blob)
    conn.close()
    return data


def base_inputs(t, d, min_mcap):
    """Same filters/EV as magicformula._compute_mf_metrics, minus the capital part."""
    prof = d.get("profile")
    inc = d.get("income-statement")
    bal = d.get("balance-sheet-statement")
    if not prof or not inc or not bal or len(inc) < 4:
        return None
    prof = prof[0]
    if prof.get("sector") in mf.EXCLUDE_SECTORS or prof.get("industry") in mf.EXCLUDE_INDUSTRIES:
        return None
    name = (prof.get("companyName") or "").lower()
    if any(x in name for x in ["preferred", "perpetual", "series a", "series b"]):
        return None
    if mf._check_income_quality(inc[:4], require_quarters=True):
        return None
    if mf.REQUIRE_PROFITABLE_LATEST_QUARTER and (inc[0].get("operatingIncome") or 0) <= 0:
        return None

    ebit = sum(q.get("operatingIncome") or 0 for q in inc[:4])
    L = lambda f: mf._latest(bal, f)
    tca, tcl, ppe, cash = L("totalCurrentAssets"), L("totalCurrentLiabilities"), \
        L("propertyPlantEquipmentNet"), L("cashAndShortTermInvestments")
    debt = L("totalDebt")
    std = L("shortTermDebt") or 0.0
    ltd = L("longTermDebt") or 0.0
    if debt is None:
        debt = std + ltd
    if (std + ltd) > debt * 1.01 + 1e6:
        return None
    mcap = prof.get("mktCap") if prof.get("mktCap") is not None else prof.get("marketCap")
    try:
        mcap = float(mcap) if mcap is not None else None
    except Exception:
        mcap = None
    if None in (tca, tcl, ppe, cash, mcap) or mcap < min_mcap:
        return None
    ev = mcap + debt - cash
    if ev <= 0 or ev < mcap * 0.01 or ebit < 0:
        return None
    return {"ticker": t, "ebit": ebit, "ev": ev, "tca": tca, "tcl": tcl, "ppe": ppe,
            "cash": cash, "std": std, "gw": L("goodwill") or 0,
            "intang": L("intangibleAssets") or 0, "ta": L("totalAssets")}


def capital(r, v):
    if v.get("method") == "assets":
        if r["ta"] is None:
            return None
        return r["ta"] - r["cash"] - (r["tcl"] - r["std"])
    nwc = (r["tca"] - (0 if v.get("cash_in") else r["cash"])) - (r["tcl"] - r["std"])
    if v.get("clamp", True):
        nwc = max(nwc, 0)
    cap = nwc + r["ppe"]
    if v.get("gw"):
        cap += r["gw"]
    if v.get("intang"):
        cap += r["intang"]
    return cap


def rank_variant(rows, v, cap):
    recs = []
    for r in rows:
        c = capital(r, v)
        if c is None or c < 10e6:
            continue
        roc = r["ebit"] / c
        if roc > 10.0:
            continue
        recs.append({"ticker": r["ticker"], "EY": r["ebit"] / r["ev"], "ROC": roc})
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df["EY_rank"] = df["EY"].rank(ascending=False, method="min")
    roc_for_rank = df["ROC"].clip(upper=cap) if cap else df["ROC"]
    df["ROC_rank"] = roc_for_rank.rank(ascending=False, method="min")
    df["MF_score"] = df["EY_rank"] + df["ROC_rank"]
    return df.sort_values(["MF_score", "EY_rank", "ROC_rank"]).reset_index(drop=True)


def diagnose(t, d, min_mcap):
    """Return the first reason a ticker is filtered out under the baseline, or None."""
    if not d:
        return "not in vault"
    prof, inc, bal = d.get("profile"), d.get("income-statement"), d.get("balance-sheet-statement")
    if not prof:
        return "no profile blob"
    if not inc or len(inc) < 4:
        return f"fewer than 4 quarters of income data ({len(inc or [])})"
    if not bal:
        return "no balance-sheet blob"
    prof = prof[0]
    if prof.get("sector") in mf.EXCLUDE_SECTORS:
        return f"excluded sector: {prof.get('sector')}"
    if prof.get("industry") in mf.EXCLUDE_INDUSTRIES:
        return f"excluded industry: {prof.get('industry')}"
    name = (prof.get("companyName") or "").lower()
    if any(x in name for x in ["preferred", "perpetual", "series a", "series b"]):
        return "name looks like a preferred/series share"
    bad = mf._check_income_quality(inc[:4], require_quarters=True)
    if bad:
        return bad
    if mf.REQUIRE_PROFITABLE_LATEST_QUARTER and (inc[0].get("operatingIncome") or 0) <= 0:
        return "operating loss in most recent quarter"
    ebit = sum(q.get("operatingIncome") or 0 for q in inc[:4])
    L = lambda f: mf._latest(bal, f)
    vals = {"tca": L("totalCurrentAssets"), "tcl": L("totalCurrentLiabilities"),
            "ppe": L("propertyPlantEquipmentNet"), "cash": L("cashAndShortTermInvestments")}
    missing = [k for k, v in vals.items() if v is None]
    mcap = prof.get("mktCap") if prof.get("mktCap") is not None else prof.get("marketCap")
    try:
        mcap = float(mcap) if mcap is not None else None
    except Exception:
        mcap = None
    if mcap is None:
        missing.append("mcap")
    if missing:
        return "missing fields: " + ", ".join(missing)
    if mcap < min_mcap:
        return f"market cap {mcap / 1e6:.0f}M below minimum"
    debt = L("totalDebt")
    std, ltd = L("shortTermDebt") or 0.0, L("longTermDebt") or 0.0
    if debt is None:
        debt = std + ltd
    if (std + ltd) > debt * 1.01 + 1e6:
        return "totalDebt below short+long-term debt"
    ev = mcap + debt - vals["cash"]
    if ev <= 0 or ev < mcap * 0.01:
        return f"EV too small/negative ({ev / 1e6:.0f}M vs mcap {mcap / 1e6:.0f}M)"
    if ebit < 0:
        return f"negative TTM EBIT ({ebit / 1e6:.1f}M)"
    nwc = max((vals["tca"] - vals["cash"]) - (vals["tcl"] - std), 0)
    cap = nwc + vals["ppe"]
    if cap < 10e6:
        return f"capital {cap / 1e6:.1f}M below $10M floor"
    if ebit / cap > 10.0:
        return "ROC above 1000%"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(mf.DB_PATH))
    ap.add_argument("--min-mcap", type=float, default=50e6)
    ap.add_argument("--detail", action="store_true",
                    help="show rank of each official ticker under the baseline")
    args = ap.parse_args()

    data = load_vault(args.db)
    rows = [r for t, d in data.items() if (r := base_inputs(t, d, args.min_mcap))]
    print(f"{len(data)} vault tickers, {len(rows)} pass the shared filters "
          f"(mcap >= {args.min_mcap:,.0f}, sanity checks, exclusions)\n")
    print(f"Official picks hit (of 30; {30 - len(FOREIGN)} if foreign names are excluded)\n")
    print(f"{'variant':32} {'cap':>5} {'valid':>6} {'top30':>6} {'top100':>7}")
    print("-" * 60)

    baseline = None
    for name, v in VARIANTS:
        for cap in CAPS:
            df = rank_variant(rows, v, cap)
            if df.empty:
                continue
            top30 = set(df.head(30)["ticker"]) & OFFICIAL
            top100 = set(df.head(100)["ticker"]) & OFFICIAL
            print(f"{name:32} {('100%' if cap else 'none'):>5} {len(df):>6} "
                  f"{len(top30):>6} {len(top100):>7}")
            if name.startswith("baseline") and cap:
                baseline = df
        print()

    if args.detail and baseline is not None:
        pos = {t: i + 1 for i, t in enumerate(baseline["ticker"])}
        print("Official picks under baseline + 100% cap (rank, or '-' if filtered out):")
        for t in sorted(OFFICIAL, key=lambda x: pos.get(x, 10**6)):
            if t in pos:
                print(f"  {t:6} {pos[t]}")
            else:
                print(f"  {t:6} -  ({diagnose(t, data.get(t), args.min_mcap) or 'passes filters?'})")


if __name__ == "__main__":
    main()
