#!/usr/bin/env python3
"""Command-line helper.

    python manage.py reset                 # fresh database + benchmark master data (nothing transacted)
    python manage.py benchmark [--export]  # reset + run all 9 benchmark steps + verify (+ write report files)
    python manage.py run [--port 5000]     # start the web app
"""
import argparse
import os
import sys

from app import create_app, reset_database
from app.db import connect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["reset", "benchmark", "run", "verify", "demo"])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--export", action="store_true", help="write all reports to ./exports (benchmark)")
    a = ap.parse_args()
    app = create_app()
    path = app.config["DATABASE"]

    if a.cmd == "run":
        if not os.path.exists(path):
            reset_database(path)
            conn = connect(path)
            from app import benchmark
            benchmark.run_benchmark(conn, log=lambda *_: None)
            from app.seed_extra import seed_extra
            seed_extra(conn)
            print("Database created and seeded with benchmark & comprehensive sample data.")
        app.run(host=a.host, port=a.port, debug=False)
        return

    if a.cmd == "reset":
        reset_database(path)
        print(f"Database reset: {path} (master data + opening stock loaded)")
        return

    from app import benchmark
    if a.cmd in ("benchmark", "demo"):
        reset_database(path)
    conn = connect(path)
    if a.cmd in ("benchmark", "demo"):
        benchmark.run_benchmark(conn)
        if a.cmd == "demo":
            from app.seed_extra import seed_extra
            seed_extra(conn)
            print("\nComprehensive demo data loaded across all modules.")
        if a.export:
            from app.services import exports
            n = exports.export_all(conn, "exports")
            print(f"Wrote {n} report files to ./exports")
    print("\nBenchmark verification")
    bad = 0
    for c in benchmark.verify(conn):
        print(f"  [{c['status']:^7}] {c['check']:<60} expected {c['expected']:<18} actual {c['actual']}")
        bad += c["status"] == "FAIL"
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
