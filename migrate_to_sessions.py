"""
One-time migration: move the existing (89th-session) data into docs/data/89/
and tag existing database rows as session 89. Safe to re-run.

Usage (from the State Bills folder):
    python migrate_to_sessions.py
"""

import shutil
import sqlite3
from pathlib import Path

import session_config as sc

OLD = sc.DOCS_DATA
NEW = sc.data_dir("89")
MOVE = ["manifest.json", "last_updated.json", "weekly_digest.md", "weekly_digest.json", "bills"]


def main():
    NEW.mkdir(parents=True, exist_ok=True)
    for name in MOVE:
        src, dst = OLD / name, NEW / name
        if not src.exists():
            continue
        if dst.exists():
            print(f"  already at data/89/{name} — leaving {name} where it is")
            continue
        shutil.move(str(src), str(dst))
        print(f"  moved data/{name} -> data/89/{name}")

    db = Path(__file__).parent / "bill_tracker.db"
    if db.exists():
        conn = sqlite3.connect(db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bills)")]
        if "session" not in cols:
            conn.execute("ALTER TABLE bills ADD COLUMN session TEXT DEFAULT '89'")
        conn.execute("UPDATE bills SET session = '89' WHERE session IS NULL OR session = ''")
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM bills WHERE session = '89'").fetchone()[0]
        print(f"  tagged {n} database row(s) as session 89")
        conn.close()

    sc.write_sessions_index()
    print("  wrote data/sessions.json\nDone.")


if __name__ == "__main__":
    main()
