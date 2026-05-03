from flask import Flask, render_template, request, Response, jsonify, send_file
import json
import threading
import queue
import time
import os
import sys
import io
import uuid
import random
import pandas as pd
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add current directory to path so magicformula imports correctly
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import magicformula as mf

app = Flask(__name__)

# In-memory store for scan state keyed by scan_id
# Each entry: {"queue": Queue, "results": None, "error": None, "done": False}
_scans = {}
_scans_lock = threading.Lock()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    api_key_present = bool(os.getenv("FMP_API_KEY"))
    return render_template("index.html", api_key_present=api_key_present)


@app.route("/scan", methods=["POST"])
def start_scan():
    """Receive scan parameters, kick off background thread, return scan_id."""
    params = request.get_json(force=True)

    # Set API key if provided via form
    api_key = params.get("api_key") or os.getenv("FMP_API_KEY", "")
    if not api_key:
        return jsonify({"error": "No FMP API key provided."}), 400
    os.environ["FMP_API_KEY"] = api_key

    scan_id = str(uuid.uuid4())
    q = queue.Queue()

    with _scans_lock:
        _scans[scan_id] = {"queue": q, "results": None, "error": None, "summary": None, "done": False,
                           "created_at": time.time(), "cancelled": False}

    t = threading.Thread(target=_run_scan, args=(scan_id, params, q), daemon=True)
    t.start()

    return jsonify({"scan_id": scan_id})

@app.route("/stop/<scan_id>", methods=["POST"])
def stop_scan(scan_id):
    with _scans_lock:
        entry = _scans.get(scan_id)
    if not entry:
        return jsonify({"error": "Unknown scan ID"}), 404
    entry["cancelled"] = True
    return jsonify({"status": "cancelling"})

@app.route("/progress/<scan_id>")
def progress(scan_id):
    """SSE endpoint — browser connects here and receives newline-delimited JSON events."""
    def generate():
        with _scans_lock:
            entry = _scans.get(scan_id)
        if not entry:
            yield _sse({"type": "error", "message": "Unknown scan ID"})
            return

        q = entry["queue"]
        while True:
            try:
                msg = q.get(timeout=30)
            except queue.Empty:
                # Send keepalive comment so the connection stays open
                yield ": keepalive\n\n"
                continue

            yield _sse(msg)

            if msg.get("type") in ("done", "error"):
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/results/<scan_id>")
def get_results(scan_id):
    """Return the final ranked results as JSON once the scan is done."""
    with _scans_lock:
        entry = _scans.get(scan_id)
    if not entry:
        return jsonify({"error": "Unknown scan ID"}), 404
    if not entry["done"]:
        return jsonify({"error": "Scan not yet complete"}), 202
    if entry["error"]:
        return jsonify({"error": entry["error"]}), 500
    return jsonify({"results": entry["results"]})


@app.route("/download/<scan_id>")
def download_csv(scan_id):
    """Stream the results as a CSV download."""
    with _scans_lock:
        entry = _scans.get(scan_id)
    if not entry or not entry["done"] or not entry["results"]:
        return jsonify({"error": "No results available"}), 404

    df = pd.DataFrame(entry["results"])
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    timestamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M")
    return send_file(
        io.BytesIO(csv_bytes),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"magic_formula_{timestamp}.csv"
    )

# ── Single stock detail page — queries raw_json_vault and company_cache ──────
@app.route("/stock/<ticker>")
def stock_detail(ticker):
    ticker = ticker.upper()
    conn = sqlite3.connect(mf.DB_PATH)

    # get all blobs for this ticker
    rows = conn.execute(
        "SELECT endpoint, json_blob, last_updated FROM raw_json_vault WHERE ticker = ?",
        (ticker,)
    ).fetchall()

    # get MF metrics from company_cache
    mf_row = conn.execute(
        "SELECT EY, ROC FROM company_cache WHERE ticker = ? ORDER BY last_updated DESC LIMIT 1",
        (ticker,)
    ).fetchone()

    conn.close()

    if not rows:
        return render_template("stock.html", ticker=ticker, not_found=True)

    data = {row[0]: json.loads(row[1]) for row in rows}
    last_updated = rows[0][2]

    return render_template("stock.html",
        ticker=ticker,
        data=data,
        last_updated=last_updated,
        mf_row=mf_row,
        not_found=False
    )

# ── Background scan logic ──────────────────────────────────────────────────────

def _push(q, msg: dict):
    q.put(msg)


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"

def _compute_summary(results: list, elapsed: float) -> dict:
    if not results:
        return {}
    eys   = [r["EY"]  for r in results if r.get("EY")  is not None]
    rocs  = [r["ROC"] for r in results if r.get("ROC") is not None]
    mcaps = [r["marketCap"] for r in results if r.get("marketCap") is not None]
    from collections import Counter
    sectors = Counter(r.get("sector") or "Unknown" for r in results)
    minutes, seconds = divmod(int(elapsed), 60)
    return {
        "elapsed_str":    f"{minutes}m {seconds}s",
        "count":          len(results),
        "avg_ey":         round(sum(eys)  / len(eys)  * 100, 2) if eys  else None,
        "avg_roc":        round(sum(rocs) / len(rocs) * 100, 2) if rocs else None,
        "median_ey":      round(sorted(eys)[len(eys)   // 2] * 100, 2) if eys  else None,
        "median_roc":     round(sorted(rocs)[len(rocs) // 2] * 100, 2) if rocs else None,
        "max_ey":         round(max(eys)  * 100, 2) if eys  else None,
        "max_roc":        round(max(rocs) * 100, 2) if rocs else None,
        "median_mcap_b":  round(sorted(mcaps)[len(mcaps) // 2] / 1e9, 2) if mcaps else None,
        "top_sectors":    dict(sectors.most_common(5)),
        "scatter": [
            {"ticker": r["ticker"], "name": r.get("name", ""), "sector": r.get("sector", ""),
             "ey": round(r["EY"] * 100, 2), "roc": round(r["ROC"] * 100, 2)}
            for r in results if r.get("EY") is not None and r.get("ROC") is not None
        ],
    }

def _evict_old_scans(max_age_seconds=7200):
    cutoff = time.time() - max_age_seconds
    with _scans_lock:
        to_del = [sid for sid, e in _scans.items()
                  if e["done"] and e.get("created_at", 0) < cutoff]
        for sid in to_del:
            del _scans[sid]

def _run_scan(scan_id: str, params: dict, q: queue.Queue):
    scan_start = time.time()

    try:
        exchanges_raw = params.get("exchanges", "NASDAQ,NYSE,AMEX")
        exchanges_list = [x.strip() for x in exchanges_raw.split(",") if x.strip()]
        min_mcap = float(params.get("min_mcap", 50_000_000))
        limit = int(params.get("limit", 4000))
        top_n = int(params.get("top_n", 30))
        use_random = params.get("use_random", True)
        use_annual = params.get("use_annual", True)
        check_debt_revenue = params.get("check_debt_revenue", False)
        check_cashflow = params.get("check_cashflow", False)

        # Country filter
        selected_countries = params.get("selected_countries", ["US"])
        if not selected_countries:
            selected_countries = ["US"]

        # ── Step 1: Gather symbols ─────────────────────────────────────────
        _push(q, {"type": "status", "message": "Gathering symbols from exchanges…", "step": 1})

        all_symbols = []
        for ex in exchanges_list:
            try:
                rows = mf.list_symbols(ex, min_mcap, selected_countries)
                for r in rows:
                    sym = r.get("symbol")
                    if sym:
                        all_symbols.append(sym)
            except Exception as e:
                _push(q, {"type": "warning", "message": f"Error fetching symbols from {ex}: {e}"})

        all_symbols = list(dict.fromkeys(all_symbols))  # dedup, preserve order

        if _scans[scan_id].get("cancelled"):
            _push(q, {"type": "error", "message": "Scan cancelled."})
            _finalize(scan_id, error="Cancelled")
            return

        if use_random:
            random.shuffle(all_symbols)

        if limit and len(all_symbols) > limit:
            all_symbols = all_symbols[:limit]

        if not all_symbols:
            _push(q, {"type": "error", "message": "No symbols found. Check exchange codes and market cap filter."})
            _finalize(scan_id, error="No symbols found.")
            return

        _push(q, {"type": "status", "message": f"Found {len(all_symbols)} symbols to analyze", "step": 1})

        # ── Step 2: Pull fundamentals ──────────────────────────────────────
        _push(q, {"type": "status", "message": "Pulling fundamentals…", "step": 2})

        records = []
        skipped = 0
        filtered = 0
        qualified = 0
        total = len(all_symbols)
        completed = 0

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(mf.fetch_company_with_cache, sym, use_annual, False): sym
                for sym in all_symbols
            }
            for future in as_completed(futures):
                sym = futures[future]
                completed += 1
                pct = round(completed / total * 100)

                if _scans[scan_id].get("cancelled"):
                    _push(q, {"type": "error", "message": "Scan cancelled."})
                    _finalize(scan_id, error="Cancelled")
                    return

                try:
                    rec = future.result(timeout=5)
                    if rec and rec.get("marketCap", 0) >= min_mcap:
                        records.append(rec)
                        qualified += 1
                    elif rec:
                        filtered += 1  # returned data but failed mcap
                    else:
                        skipped += 1  # returned None — missing/incomplete data
                except Exception:
                    skipped += 1

                _push(q, {
                    "type": "progress",
                    "symbol": sym,
                    "completed": completed,
                    "total": total,
                    "pct": pct,
                    "skipped": skipped,
                    "filtered": filtered,
                    "qualified": qualified,
                })

        if not records:
            _push(q, {"type": "error", "message": "No qualifying stocks found. Try lowering min market cap or increasing scan limit."})
            _finalize(scan_id, error="No qualifying stocks found.")
            return

        _push(q, {"type": "status", "message": f"Found {len(records)} qualifying stocks. Ranking…", "step": 3})

        # ── Step 3: Rank ───────────────────────────────────────────────────
        df = pd.DataFrame(records)
        ranked = mf.magic_formula_rank(df)
        priority_cols = ["ticker", "name", "EY", "ROC", "exchange", "industry", "country", "MF_score"]
        remaining_cols = [c for c in ranked.columns if c not in priority_cols]
        ranked = ranked[priority_cols + remaining_cols]

        # ── Step 4: Optional health checks ────────────────────────────────
        if check_debt_revenue or check_cashflow:
            _push(q, {"type": "status", "message": f"Running health checks on top {top_n} candidates…", "step": 4})
            top_candidates = ranked.head(top_n)
            healthy_tickers = []
            for ticker in top_candidates["ticker"]:
                health = mf.check_financial_health(
                    ticker,
                    check_debt_revenue=check_debt_revenue,
                    check_cashflow_quality=check_cashflow
                )
                if health["passes_all"]:
                    healthy_tickers.append(ticker)
            ranked = ranked[ranked["ticker"].isin(healthy_tickers)]
            _push(q, {"type": "status", "message": f"Health checks: {len(healthy_tickers)}/{len(top_candidates)} passed", "step": 4})

        # ── Step 5: Build output ───────────────────────────────────────────
        display_cols = [
            "ticker", "name", "exchange", "country", "sector", "industry",
            "marketCap", "EV", "EBIT", "EY", "ROC", "EY_rank", "ROC_rank", "MF_score"
        ]
        display_cols = [c for c in display_cols if c in ranked.columns]
        final_df = ranked[display_cols].head(top_n)

        results = final_df.to_dict(orient="records")

        elapsed = time.time() - scan_start
        summary = _compute_summary(results, elapsed)
        _finalize(scan_id, results=results, summary=summary)
        _push(q, {"type": "done", "count": len(results), "total_analyzed": len(records), "summary": summary})

    except Exception as e:
        _push(q, {"type": "error", "message": str(e)})
        _finalize(scan_id, error=str(e))


def _finalize(scan_id: str, results=None, error=None, summary=None):
    with _scans_lock:
        if scan_id in _scans:
            _scans[scan_id]["results"] = results
            _scans[scan_id]["error"] = error
            _scans[scan_id]["summary"] = summary
            _scans[scan_id]["done"] = True


# ── Entry point ───────────────────────────────────────────────────────────────

def _cleanup_loop():
    while True:
        time.sleep(3600)
        _evict_old_scans()

threading.Thread(target=_cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True, use_reloader=False)
