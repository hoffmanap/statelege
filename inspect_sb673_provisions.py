"""
Run from your State Bills folder. Shows every provision SB673 actually
split into, and which (if any) regulatory categories were detected in
each — so we can see whether this is a vocabulary-mismatch problem
(the bill uses different words than our keyword list expects) rather
than another pipeline bug.
"""

import re
import sqlite3
from pathlib import Path

from prescreen_conflicts import (
    extract_plain_text, split_bill_provisions, detect_categories, CATEGORIES
)

conn = sqlite3.connect("bill_tracker.db")
bill_id = conn.execute("SELECT bill_id FROM bills WHERE bill_number = 'SB673'").fetchone()[0]
text_path = conn.execute(
    "SELECT text_path FROM bill_versions WHERE bill_id = ? ORDER BY doc_date DESC LIMIT 1",
    (bill_id,)
).fetchone()[0]

bill_text = extract_plain_text(Path(text_path))
provisions = split_bill_provisions(bill_text)

print(f"Total provisions parsed: {len(provisions)}\n")

for p in provisions:
    cats = detect_categories(p["text"])
    print(f"HEADING: {p['heading']}")
    print(f"  categories detected: {cats if cats else '(none)'}")
    print(f"  first 200 chars: {p['text'][:200]}")
    print("-" * 60)
