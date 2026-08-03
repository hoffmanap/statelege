"""
LegiScan weekly ingestion script — El Paso housing/zoning bill monitor
90th Texas Legislature (2027) prep

Purpose:
  - Pull TX bills matching housing/zoning/subdivision/building-code keywords
  - Track change_hash per bill so we only reprocess bills that actually changed
  - Always fetch the LATEST bill draft (not just the first one found)
  - Store raw text + metadata locally for the downstream conflict-scoring pipeline

Usage:
  export LEGISCAN_API_KEY="your_key_here"
  python legiscan_ingest.py

Run this weekly via cron. It's idempotent — safe to re-run same week.
"""

import os
import json
import base64
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_KEY = os.environ.get("LEGISCAN_API_KEY")
BASE_URL = "https://api.legiscan.com/"
STATE = "TX"

# Keyword sets — used to filter LegiScan's TX master list to relevant bills.
# LegiScan's getSearch supports a query string; we run one search per topic
# and de-dupe by bill_id, since a single combined query returns weaker matches
# for each individual topic.
TOPIC_QUERIES = [
    "zoning",
    "subdivision plat",
    "building code",
    "accessory dwelling unit",
    "manufactured home",
    "residential lot size",
    "impact fee",
    "permitting municipality",
    "multifamily residential development",
    "setback density",
    "short-term rental municipality",
    "manufactured housing",
]

DB_PATH = Path(__file__).parent / "bill_tracker.db"
TEXT_STORE = Path(__file__).parent / "bill_texts"
TEXT_STORE.mkdir(exist_ok=True)

# 90th Texas Legislature regular session window. Update these two dates when
# the session convenes/adjourns (and again for any special sessions called).
# This is what lets us call a bill "dead" even when LegiScan hasn't formally
# marked it Failed — many bills just quietly stop moving and never get a
# terminal status code.
SESSION_START = datetime(2027, 1, 12, tzinfo=timezone.utc)
SESSION_END = datetime(2027, 6, 1, tzinfo=timezone.utc)  # approx sine die + buffer

# LegiScan status codes: 1=Introduced, 2=Engrossed, 3=Enrolled,
# 4=Passed, 5=Vetoed, 6=Failed/Dead
LEGISCAN_PASSED = 4
LEGISCAN_DEAD = (5, 6)


def session_is_active(now=None):
    now = now or datetime.now(timezone.utc)
    return SESSION_START <= now <= SESSION_END


def bill_lifecycle(status_code):
    """
    Classify a bill into active / passed / dead. This is the one place we
    deviate from LegiScan's raw status — everything else (progress detail,
    stage labels) should just pass through LegiScan's data as-is.
    """
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        status_code = None

    if status_code in LEGISCAN_DEAD:
        return "dead"
    if status_code == LEGISCAN_PASSED:
        return "passed"
    if not session_is_active():
        return "dead"  # session over, bill never formally advanced
    return "active"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            bill_id INTEGER PRIMARY KEY,
            bill_number TEXT,
            title TEXT,
            status TEXT,
            last_action TEXT,
            last_action_date TEXT,
            change_hash TEXT,
            last_checked TEXT,
            latest_doc_id INTEGER,
            latest_doc_type TEXT,
            lifecycle TEXT,
            legiscan_status_code INTEGER,
            needs_review INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bill_versions (
            doc_id INTEGER PRIMARY KEY,
            bill_id INTEGER,
            doc_type TEXT,
            doc_date TEXT,
            fetched_at TEXT,
            text_path TEXT
        )
    """)
    return conn


def api_call(op, **params):
    """Wrapper around LegiScan API with basic rate-limit courtesy."""
    params.update({"key": API_KEY, "op": op})
    resp = requests.get(BASE_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "OK":
        raise RuntimeError(f"LegiScan API error for op={op}: {data}")
    time.sleep(0.5)  # be polite to the API
    return data


def search_bills(query):
    """getSearch: full text + metadata search scoped to TX, current session."""
    data = api_call("getSearch", state=STATE, query=query)
    results = data.get("searchresult", {})
    bills = []
    for key, val in results.items():
        if key == "summary":
            continue
        bills.append(val)
    return bills


def get_bill_detail(bill_id):
    """getBill: full bill metadata, including all text version doc_ids."""
    data = api_call("getBill", id=bill_id)
    return data.get("bill", {})


def get_bill_text(doc_id):
    """getBillText: returns base64-encoded doc (usually PDF or HTML)."""
    data = api_call("getBillText", id=doc_id)
    return data.get("text", {})


def latest_version(bill_detail):
    """
    Pick the most recent bill text version by date, not by list order.
    bill_detail['texts'] is a list of dicts with doc_id, type, date.
    """
    texts = bill_detail.get("texts", [])
    if not texts:
        return None
    return max(texts, key=lambda t: t.get("date", ""))


def extract_text_bytes(text_obj):
    """Decode LegiScan's base64 doc payload. Handles PDF or HTML mime types."""
    raw = base64.b64decode(text_obj["doc"])
    mime = text_obj.get("mime", "")
    return raw, mime


def save_text_to_disk(bill_number, doc_id, raw_bytes, mime):
    ext = "pdf" if "pdf" in mime.lower() else "html"
    path = TEXT_STORE / f"{bill_number}_{doc_id}.{ext}"
    path.write_bytes(raw_bytes)
    return str(path)


def is_relevant(bill_summary):
    """
    Cheap pre-filter before pulling full detail: skip obviously irrelevant
    bills that only matched on a coincidental keyword. Refine over time —
    for now just pass everything through; the real filtering happens in the
    downstream matching/scoring step against your city code corpus.
    """
    return True


def write_bill_base_json(bill_number, bill_id, title, lifecycle, status, history, legiscan_url):
    """
    Write (or refresh) data/bills/{bill_number}.json with LegiScan-native
    fields — status, history/progress, url. score_conflicts.py loads this
    same file and MERGES its own conflicts/gaps/severity fields in rather
    than overwriting, so run order (ingest then score) matters but neither
    script clobbers the other's fields.
    """
    out_dir = Path(__file__).parent / "docs" / "data" / "bills"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{bill_number}.json"

    existing = {}
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))

    existing.update({
        "bill_id": bill_id,
        "bill_number": bill_number,
        "title": title,
        "lifecycle": lifecycle,
        "legiscan_status": status,
        "legiscan_url": legiscan_url,
        # Pass LegiScan's progress history through as-is — date, action,
        # chamber — per the earlier decision not to reinvent their tracking.
        "progress": history,
    })
    # Preserve conflicts/gaps/overall_severity/scope if score_conflicts.py
    # already ran and populated them on a previous pass.
    existing.setdefault("conflicts", [])
    existing.setdefault("gaps", [])
    existing.setdefault("overall_severity", None)
    existing.setdefault("scope", None)

    out_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def run():
    if not API_KEY:
        raise SystemExit("Set LEGISCAN_API_KEY environment variable first.")

    conn = get_conn()
    seen_bill_ids = set()
    new_or_changed = []

    for query in TOPIC_QUERIES:
        print(f"Searching: {query!r}")
        try:
            results = search_bills(query)
        except Exception as e:
            print(f"  search failed for {query!r}: {e}")
            continue

        for r in results:
            bill_id = r.get("bill_id") or r.get("Bill_ID")
            if not bill_id or bill_id in seen_bill_ids:
                continue
            seen_bill_ids.add(bill_id)

            if not is_relevant(r):
                continue

            # Check change_hash against what we've stored
            row = conn.execute(
                "SELECT change_hash FROM bills WHERE bill_id = ?", (bill_id,)
            ).fetchone()
            incoming_hash = r.get("change_hash") or r.get("relevance")  # search result hash field varies

            try:
                detail = get_bill_detail(bill_id)
            except Exception as e:
                print(f"  could not fetch detail for bill {bill_id}: {e}")
                continue

            actual_hash = detail.get("change_hash")
            bill_number = detail.get("bill_number", "")
            title = detail.get("title", "")
            status_code = detail.get("status")
            status = str(status_code)
            lifecycle = bill_lifecycle(status_code)
            last_action = detail.get("history", [{}])[-1].get("action", "") if detail.get("history") else ""
            last_action_date = detail.get("history", [{}])[-1].get("date", "") if detail.get("history") else ""

            unchanged = row is not None and row[0] == actual_hash
            if unchanged:
                continue  # nothing new since last run

            # Fetch the latest text version only
            lv = latest_version(detail)
            text_path = None
            if lv:
                try:
                    text_obj = get_bill_text(lv["doc_id"])
                    raw, mime = extract_text_bytes(text_obj)
                    text_path = save_text_to_disk(bill_number, lv["doc_id"], raw, mime)
                except Exception as e:
                    print(f"  could not fetch text for bill {bill_id} doc {lv.get('doc_id')}: {e}")

            conn.execute("""
                INSERT INTO bills (bill_id, bill_number, title, status, last_action,
                                    last_action_date, change_hash, last_checked,
                                    latest_doc_id, latest_doc_type, lifecycle,
                                    legiscan_status_code, needs_review)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(bill_id) DO UPDATE SET
                    title=excluded.title,
                    status=excluded.status,
                    last_action=excluded.last_action,
                    last_action_date=excluded.last_action_date,
                    change_hash=excluded.change_hash,
                    last_checked=excluded.last_checked,
                    latest_doc_id=excluded.latest_doc_id,
                    latest_doc_type=excluded.latest_doc_type,
                    lifecycle=excluded.lifecycle,
                    legiscan_status_code=excluded.legiscan_status_code,
                    needs_review=1
            """, (
                bill_id, bill_number, title, status, last_action, last_action_date,
                actual_hash, datetime.now(timezone.utc).isoformat(),
                lv["doc_id"] if lv else None, lv.get("type") if lv else None,
                lifecycle, status_code,
            ))
            conn.commit()

            if lv:
                conn.execute("""
                    INSERT OR IGNORE INTO bill_versions
                        (doc_id, bill_id, doc_type, doc_date, fetched_at, text_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    lv["doc_id"], bill_id, lv.get("type"), lv.get("date"),
                    datetime.now(timezone.utc).isoformat(), text_path,
                ))
                conn.commit()

            new_or_changed.append(bill_number)
            print(f"  NEW/CHANGED: {bill_number} — {title[:80]}")

            write_bill_base_json(bill_number, bill_id, title, lifecycle, status,
                                  detail.get("history", []), detail.get("url", ""))

    print(f"\nDone. {len(new_or_changed)} bill(s) new or changed this run.")
    print("These are flagged needs_review=1 in bill_tracker.db, ready for the")
    print("code-conflict scoring step.")

    export_manifest(conn)


def export_manifest(conn):
    """
    Write data/manifest.json for the GitHub Pages front end. This is the
    lightweight index the list view loads first — full per-bill conflict
    detail (severity, code sections, required changes) gets merged in by
    the downstream scoring script.

    BUG FIX: this used to reset overall_severity/scope to null placeholders
    for EVERY bill on every run, which silently wiped out real scoring
    results from prescreen_conflicts.py for any bill that wasn't rescanned
    that particular week (i.e. most bills, most weeks, since only changed
    bills get needs_review=1). Now it loads the existing manifest first (if
    one exists) and carries forward each bill's previously-scored severity/
    scope, only defaulting to null for bills that are genuinely new.
    """
    out_dir = Path(__file__).parent / "docs" / "data"
    out_dir.mkdir(exist_ok=True)

    manifest_path = out_dir / "manifest.json"
    previous_scores = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            previous_scores = {
                b["bill_number"]: (b.get("overall_severity"), b.get("scope"))
                for b in previous
            }
        except (json.JSONDecodeError, KeyError):
            pass  # if the existing file is somehow malformed, just start fresh

    rows = conn.execute("""
        SELECT bill_number, title, lifecycle, status, last_action,
               last_action_date, bill_id
        FROM bills
        ORDER BY last_action_date DESC
    """).fetchall()

    manifest = []
    for r in rows:
        bill_number = r[0]
        prev_severity, prev_scope = previous_scores.get(bill_number, (None, None))
        manifest.append({
            "bill_id": r[6],
            "bill_number": bill_number,
            "title": r[1],
            "lifecycle": r[2],          # active / passed / dead
            "legiscan_status": r[3],
            "last_action": r[4],
            "last_action_date": r[5],
            # Carried forward from the previous manifest if this bill was
            # already scored; only null for bills seen for the first time.
            "overall_severity": prev_severity,
            "scope": prev_scope,
        })

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    (out_dir / "last_updated.json").write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat()
    }, indent=2), encoding="utf-8")

    print(f"Wrote data/manifest.json ({len(manifest)} bills) and last_updated.json")


if __name__ == "__main__":
    run()