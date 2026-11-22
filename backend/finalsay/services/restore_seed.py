"""Restore pre-scraped notice seed database on startup if database is empty."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3

logger = logging.getLogger(__name__)


def restore_if_needed() -> None:
    seed_candidates = [
        "/app/finalsay_seed.db.gz",
        os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "finalsay_seed.db.gz")
        ),
        "finalsay_seed.db.gz",
    ]
    seed_path = next((p for p in seed_candidates if os.path.exists(p)), None)
    if not seed_path:
        return

    # Check target database path from FINALSAY_DATABASE_URL or default
    db_candidates = [
        "/app/finalsay.db",
        "finalsay.db",
    ]
    target_path = "/app/finalsay.db" if os.path.exists("/app") else "finalsay.db"

    should_restore = False
    if not os.path.exists(target_path) or os.path.getsize(target_path) < 100_000:
        should_restore = True
    else:
        try:
            conn = sqlite3.connect(target_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='notice'"
            )
            has_table = cur.fetchone()[0]
            if not has_table:
                should_restore = True
            else:
                cur.execute("SELECT count(*) FROM notice")
                count = cur.fetchone()[0]
                if count < 3000:
                    should_restore = True
            conn.close()
        except Exception:
            should_restore = True

    if should_restore:
        print(f"==> Restoring full notice seed corpus from {seed_path} to {target_path}...")
        try:
            with gzip.open(seed_path, "rb") as f_in, open(target_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            print(f"==> Restored {target_path} successfully ({os.path.getsize(target_path)} bytes).")
        except Exception as exc:
            print(f"==> Warning: Could not restore seed database: {exc}")


if __name__ == "__main__":
    restore_if_needed()
