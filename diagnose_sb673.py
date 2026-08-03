"""
Diagnostic — run this from your State Bills folder to see exactly where
SB673's pipeline is breaking down. No changes made to any data, just prints.

Usage:
    python diagnose_sb673.py
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path("bill_tracker.db")
CODE_DIR = Path("city_code")

print("=" * 70)
print("STEP 1: Is there a saved bill text file for SB673?")
print("=" * 70)

conn = sqlite3.connect(DB_PATH)
row = conn.execute("SELECT bill_id FROM bills WHERE bill_number = 'SB673'").fetchone()
if not row:
    print("SB673 not found in bill_tracker.db at all — re-run legiscan_ingest.py")
else:
    bill_id = row[0]
    vrow = conn.execute(
        "SELECT doc_id, text_path FROM bill_versions WHERE bill_id = ? ORDER BY doc_date DESC LIMIT 1",
        (bill_id,)
    ).fetchone()
    if not vrow or not vrow[1]:
        print("No text_path stored for SB673 — the text fetch/save step failed silently.")
    else:
        doc_id, text_path = vrow
        print(f"doc_id: {doc_id}")
        print(f"text_path: {text_path}")
        p = Path(text_path)
        print(f"File exists on disk: {p.exists()}")
        if p.exists():
            print(f"File size: {p.stat().st_size} bytes")
            print(f"File suffix: {p.suffix}")

print()
print("=" * 70)
print("STEP 2: Does text actually extract from that file?")
print("=" * 70)

if row and vrow and vrow[1] and Path(vrow[1]).exists():
    p = Path(vrow[1])
    if p.suffix.lower() == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(p) as pdf:
                print(f"Page count: {len(pdf.pages)}")
                text = ""
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text += t + "\n"
                print(f"Total extracted characters: {len(text)}")
                print("First 500 characters extracted:")
                print("-" * 40)
                print(text[:500])
                print("-" * 40)
        except ImportError:
            print("pdfplumber not installed — run: pip install pdfplumber --break-system-packages")
    else:
        raw = p.read_text(errors="ignore")
        print(f"Total characters (raw read): {len(raw)}")
        print("First 500 characters:")
        print("-" * 40)
        print(raw[:500])
        print("-" * 40)

print()
print("=" * 70)
print("STEP 3: Does the section-header regex find anything in that text?")
print("=" * 70)

import re
BILL_SECTION_RE = re.compile(r"(Sec\.\s+\d+[A-Za-z]?\.\d+\.\s+[A-Z][A-Z \-,']+\.)")

if row and vrow and vrow[1] and Path(vrow[1]).exists():
    p = Path(vrow[1])
    if p.suffix.lower() == ".pdf":
        import pdfplumber
        text = ""
        with pdfplumber.open(p) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
    else:
        raw = p.read_text(errors="ignore")
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text)

    matches = BILL_SECTION_RE.findall(text)
    print(f"Section headers matched: {len(matches)}")
    for m in matches[:15]:
        print(f"  - {m}")
    if not matches:
        print("NO MATCHES. The regex expects 'Sec. 123.456. ALL CAPS HEADING.' — "
              "if this bill's PDF text extraction mangles spacing or the heading "
              "isn't in that exact format, nothing will split into provisions.")

print()
print("=" * 70)
print("STEP 4: Does the city_code corpus contain an ADU section (20.10.035)?")
print("=" * 70)

if not CODE_DIR.exists():
    print("city_code/ folder not found.")
else:
    found = list(CODE_DIR.rglob("20.10.035.json"))
    if found:
        print(f"Found: {found[0]}")
        print(json.loads(found[0].read_text())["text"][:300])
    else:
        print("NOT FOUND. Searching for any file mentioning 'accessory dwelling'...")
        hits = []
        for path in CODE_DIR.rglob("*.json"):
            try:
                content = path.read_text()
                if "accessory dwelling" in content.lower() or "adu" in content.lower():
                    hits.append(path)
            except Exception:
                pass
        if hits:
            print(f"Found {len(hits)} file(s) mentioning ADU under a different name:")
            for h in hits[:10]:
                print(f"  - {h}")
        else:
            print("No file in city_code/ mentions accessory dwelling units at all. "
                  "This means your source docx for Title 20 either doesn't include "
                  "§20.10.035, or it wasn't extracted by code_to_json.py — check the "
                  "source file and re-run the conversion.")

print()
print("=" * 70)
print("Done. Share this output to pinpoint exactly which step is failing.")
print("=" * 70)
