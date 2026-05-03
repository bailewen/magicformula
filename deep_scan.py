
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from magicformula import DB_PATH, fmp_get, _init_db

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
    # get all tickers from company_cache
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT ticker FROM company_cache").fetchall()
    conn.close()

    tickers = [r[0] for r in rows]
    if not tickers:
        print("No tickers in company_cache. Run the main scan first.")
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
            elif result is None:
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

# -------------------- CLI --------------------


if __name__ == "__main__":
    _init_db()
    run_deep_scan()

# TEMP TEST
#if __name__ == "__main__":
#    _init_db()
#    result = fetch_and_store("AAPL", "profile")
#    print(result)
