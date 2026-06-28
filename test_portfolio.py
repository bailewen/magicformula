"""
test_portfolio.py

Standalone end-to-end smoke test for portfolio.py.

Runs the whole data layer against a THROWAWAY copy of the database so it
never touches your real cache.db. Exercises: schema creation, CSV import
(with fund/zero-qty filtering), reading positions back, manual add, manual
delete, edit, and portfolio deletion (cascade).

Usage:
    python3 test_portfolio.py path/to/Holdings_export.csv

If no CSV path is given it runs everything except the import step, so you
can still smoke-test the CRUD paths without an export handy.

Safe to run repeatedly: it works on a temp copy and cleans up after itself.
"""

import os
import shutil
import sys
import tempfile

import magicformula as mf


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else None
    if csv_path and not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}")
        sys.exit(1)

    # ---- point the data layer at a throwaway copy of the real DB ----------
    real_db = mf.DB_PATH
    tmp_dir = tempfile.mkdtemp(prefix="portfolio_test_")
    test_db = os.path.join(tmp_dir, "test_cache.db")

    if os.path.exists(real_db):
        shutil.copy(real_db, test_db)
        print(f"Copied real DB -> {test_db}")
    else:
        print(f"(no real DB at {real_db}; starting from an empty test DB)")

    # Redirect every connection in the module to the copy. We override
    # DB_PATH *before* importing portfolio so its mf.get_conn() picks it up.
    mf.DB_PATH = test_db

    import portfolio  # imported after DB_PATH is redirected

    ok = True
    try:
        # ---- 1. schema -----------------------------------------------------
        portfolio.init_schema()
        print("\n[1] init_schema() ok")

        # ---- 2. create a portfolio ----------------------------------------
        pid = portfolio.create_portfolio("Test Merrill Main")
        print(f"[2] created portfolio id={pid}")
        assert portfolio.get_portfolio(pid) is not None

        # ---- 3. CSV import (if a path was supplied) -----------------------
        if csv_path:
            count, skipped = portfolio.import_merrill_csv(pid, csv_path)
            print(f"[3] imported {count} positions")
            if skipped:
                print("    skipped:")
                for sym, reason in skipped:
                    print(f"      - {sym}: {reason}")
        else:
            print("[3] (no CSV given, skipping import)")

        # ---- 4. read positions back ---------------------------------------
        positions = portfolio.list_positions(pid)
        print(f"[4] portfolio now holds {len(positions)} positions:")
        print(f"    {'id':>4}  {'ticker':8} {'shares':>8} {'cost/sh':>10}")
        for p in positions:
            cb = f"{p['cost_basis']:.4f}" if p["cost_basis"] is not None else "None"
            print(f"    {p['id']:>4}  {p['ticker']:8} {p['shares']:>8.0f} {cb:>10}")

        # ---- 4b. snapshots (record + upsert) --------------------------------
        mock_prices = {p["ticker"]: {"price": 100.0} for p in positions}
        portfolio.record_snapshot(pid, prices=mock_prices if mock_prices else None)
        snaps = portfolio.get_snapshots(pid)
        assert len(snaps) == 1, f"expected 1 snapshot, got {len(snaps)}"
        assert snaps[0]["portfolio_id"] == pid
        first_val = snaps[0]["total_value"]
        # second call must upsert, not insert a duplicate
        portfolio.record_snapshot(pid, prices=mock_prices if mock_prices else None)
        snaps2 = portfolio.get_snapshots(pid)
        assert len(snaps2) == 1, "second record_snapshot should upsert, not insert"
        print(f"[4b] snapshot ok — total_value={first_val}, spy={snaps[0]['spy_price']}, qqq={snaps[0]['qqq_price']}")

        # ---- 5. manual add (paper-portfolio style) ------------------------
        new_id = portfolio.add_position(pid, "nvda", 10, cost_basis=120.50)
        print(f"\n[5] manually added NVDA (lowercase -> upper), id={new_id}")
        added = next(p for p in portfolio.list_positions(pid) if p["id"] == new_id)
        assert added["ticker"] == "NVDA", "ticker should be uppercased on insert"
        assert added["shares"] == 10
        print(f"    stored as: {added['ticker']} {added['shares']:.0f} @ {added['cost_basis']}")

        # ---- 6. edit a position (share drift, e.g. DRIP) ------------------
        portfolio.update_position(new_id, shares=12)
        edited = next(p for p in portfolio.list_positions(pid) if p["id"] == new_id)
        assert edited["shares"] == 12
        print(f"[6] updated NVDA shares 10 -> {edited['shares']:.0f} (cost basis untouched: {edited['cost_basis']})")

        # ---- 7. manual delete (the - / x button) --------------------------
        before = len(portfolio.list_positions(pid))
        portfolio.delete_position(new_id)
        after = len(portfolio.list_positions(pid))
        assert after == before - 1
        print(f"[7] deleted NVDA: {before} -> {after} positions")

        # ---- 8. portfolio delete cascades to positions --------------------
        portfolio.delete_portfolio(pid)
        assert portfolio.get_portfolio(pid) is None
        assert portfolio.list_positions(pid) == []
        print(f"[8] deleted portfolio {pid}; positions gone too (cascade ok)")

        print("\nALL CHECKS PASSED")

    except Exception as e:
        ok = False
        print(f"\nFAILED: {type(e).__name__}: {e}")
        raise
    finally:
        # restore the module's DB_PATH and clean up the temp copy
        mf.DB_PATH = real_db
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"cleaned up {tmp_dir}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
