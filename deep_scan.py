
import json
import sqlite3
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from magicformula import list_symbols
from magicformula import DB_PATH, fmp_get, _init_db, compute_mf_from_vault, magic_formula_rank
# -------------------- Deep Scan --------------------


DEEP_SCAN_ENDPOINTS = [
    "profile",
    "quote",
    "income-statement",
    "balance-sheet-statement",
    "cash-flow-statement",
    "ratios-ttm",
    "key-metrics-ttm",
]

def fetch_and_store(ticker: str, endpoint: str) -> tuple | None :
    try:
        data = fmp_get(f"/{endpoint}/{ticker}")
        if not data:
            return None
        return (ticker, endpoint, json.dumps(data))
    except Exception as e:
        print(f"  [{ticker}] {endpoint}: {e}")
        return None

def run_deep_scan():
    exchanges = ["NASDAQ", "NYSE", "AMEX"]
    all_tickers = []
    for ex in exchanges:
        try:
            rows = list_symbols(ex, min_mcap=0, countries=["US"])
            all_tickers.extend(r["symbol"] for r in rows if r.get("symbol"))
        except Exception as e:
            print(f"Error fetching symbols from {ex}: {e}")

    tickers = list(dict.fromkeys(all_tickers))  # dedup
    if not tickers:
        print("No tickers found.")
        return

    total = len(tickers) * len(DEEP_SCAN_ENDPOINTS)
    print(f"Deep scan: {len(tickers)} tickers x {len(DEEP_SCAN_ENDPOINTS)} endpoints = {total} API calls")

    ok = skip = 0
    conn = sqlite3.connect(DB_PATH)
    batch = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(fetch_and_store, ticker, endpoint): (ticker, endpoint)
            for ticker in tickers
            for endpoint in DEEP_SCAN_ENDPOINTS
        }
        for future in tqdm(as_completed(futures), total=len(futures), desc="Deep scan"):
            result = future.result()
            if result is not None:
                batch.append(result)
                if len(batch) >= 500:
                    conn.executemany("""
                        INSERT INTO raw_json_vault (ticker, endpoint, json_blob)
                        VALUES (?, ?, ?)
                        ON CONFLICT(ticker, endpoint) DO UPDATE SET
                            json_blob = excluded.json_blob,
                            last_updated = CURRENT_TIMESTAMP
                    """, batch)
                    conn.commit()
                    batch = []
                ok += 1
            else:
                skip += 1

    if batch:
        conn.executemany("""
            INSERT INTO raw_json_vault (ticker, endpoint, json_blob)
            VALUES (?, ?, ?)
            ON CONFLICT(ticker, endpoint) DO UPDATE SET
                json_blob = excluded.json_blob,
                last_updated = CURRENT_TIMESTAMP
        """, batch)
        conn.commit()

    conn.close()
    print(f"Done: {ok} stored, {skip} skipped/failed")

def run_mf_universe():
    """
    After deep scan, compute EY/ROC/MF_score for all tickers in raw_json_vault
    and write to mf_universe table.
    """
    conn = sqlite3.connect(DB_PATH)

    # Create table if needed
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mf_universe (
            ticker      TEXT PRIMARY KEY,
            name        TEXT,
            exchange    TEXT,
            country     TEXT,
            sector      TEXT,
            industry    TEXT,
            marketCap   REAL,
            EV          REAL,
            EBIT        REAL,
            NWC         REAL,
            PPE_Net     REAL,
            Capital     REAL,
            Cash        REAL,
            TotalDebt   REAL,
            EY          REAL,
            ROC         REAL,
            EY_rank     INTEGER,
            ROC_rank    INTEGER,
            MF_score    INTEGER,
            Goodwill    REAL,
            Intangibles REAL,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    # Get all tickers in vault
    tickers = [r[0] for r in conn.execute(
        "SELECT DISTINCT ticker FROM raw_json_vault"
    ).fetchall()]

    print(f"Computing MF metrics for {len(tickers)} tickers...")

    records = []
    for ticker in tickers:
        rows = conn.execute(
            "SELECT endpoint, json_blob FROM raw_json_vault WHERE ticker = ?",
            (ticker,)
        ).fetchall()
        vault = {r[0]: json.loads(r[1]) for r in rows}
        result = compute_mf_from_vault(ticker, vault, annual=True)
        if result:
            records.append(result)

    print(f"Qualified: {len(records)} / {len(tickers)}")

    if not records:
        conn.close()
        return

    # Rank using existing magic_formula_rank
    df = pd.DataFrame(records)
    ranked = magic_formula_rank(df)

    # Write to mf_universe
    conn.execute("DELETE FROM mf_universe")
    ranked.to_sql("mf_universe", conn, if_exists="append", index=False)
    conn.commit()
    conn.close()
    print(f"mf_universe populated with {len(ranked)} ranked tickers.")

# -------------------- Universal MF score -----
if __name__ == "__main__":
    _init_db()
    run_deep_scan()
    run_mf_universe()


