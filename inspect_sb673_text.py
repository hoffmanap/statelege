"""
Run from your State Bills folder. Shows exactly how "Sec." appears in the
stripped SB673 text so we can write a regex that actually matches it,
instead of guessing again.
"""

import re
import sqlite3
from pathlib import Path

DB_PATH = Path("bill_tracker.db")

conn = sqlite3.connect(DB_PATH)
bill_id = conn.execute("SELECT bill_id FROM bills WHERE bill_number = 'SB673'").fetchone()[0]
text_path = conn.execute(
    "SELECT text_path FROM bill_versions WHERE bill_id = ? ORDER BY doc_date DESC LIMIT 1",
    (bill_id,)
).fetchone()[0]

raw = Path(text_path).read_text(errors="ignore")

# Strip tags the same way the pipeline does
text = re.sub(r"<[^>]+>", " ", raw)
text = re.sub(r"&#xA0;|&nbsp;", " ", text)   # HTML non-breaking space entities
text = re.sub(r"\s+", " ", text)

print(f"Total stripped length: {len(text)} characters\n")

# Find every literal occurrence of "Sec." and print surrounding context
print("=" * 70)
print("Context around every 'Sec.' occurrence:")
print("=" * 70)

for m in re.finditer(r"Sec\.", text):
    start = max(0, m.start() - 20)
    end = min(len(text), m.start() + 80)
    print(f"...{text[start:end]}...")
    print("-" * 40)
