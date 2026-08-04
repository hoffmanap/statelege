"""
Rule-based conflict pre-screen — El Paso Legislative Watch
90th Texas Legislature prep

NO LLM CALL. NO API KEY REQUIRED. NO COST TO RUN.

This replaces score_conflicts.py's Claude-based judgment with keyword +
numeric pattern matching. It does NOT determine whether a conflict is real —
it flags candidate matches where a bill provision and a city code section
both address the same regulatory category (setbacks, lot size, occupancy,
design review, etc.) and pulls out whatever numbers it can find on each
side, so a human can eyeball it fast instead of reading full bill text
cold. Every output entry has "needs_manual_review": true and a
"rule_confidence" of high/medium/low — think of this as a highlighter,
not a verdict.

For an actual conflict determination, take the flagged pairs from
data/bills/{bill}.json and paste the bill_text_excerpt + code_text_excerpt
into a regular claude.ai chat and ask directly — same quality analysis as
the API version, just done by hand instead of automatically.

Usage:
    pip install scikit-learn pdfplumber --break-system-packages
    python prescreen_conflicts.py

Run this after legiscan_ingest.py and code_to_json.py in the same weekly
job. Only bills flagged needs_review=1 get (re)screened.
"""

import json
import re
import sqlite3
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

DB_PATH = Path(__file__).parent / "bill_tracker.db"
CODE_DIR = Path(__file__).parent / "docs" / "city_code"
DATA_DIR = Path(__file__).parent / "docs" / "data"
BILLS_OUT = DATA_DIR / "bills"
BILLS_OUT.mkdir(parents=True, exist_ok=True)

TOP_K_CANDIDATES = 8  # per category-filtered pool, not the whole corpus — see top_candidates()

BILL_SECTION_RE = re.compile(r"(Sec\.\s+\d+[A-Za-z]?\.\d+\.\s+[A-Z][A-Z \-,']+\.)")

# ---------- Regulatory categories ----------
# Each category has keywords (to detect "this text is about X") and a
# priority weight used to build a rough severity proxy. Weights are a
# starting point based on what actually mattered in SB840/SB15/SB673 —
# adjust as you see how real bills shake out.
CATEGORIES = {
    "owner_occupancy": {
        "keywords": ["owner-occup", "owner occup", "primary residence", "permanent residence"],
        "weight": 5,
    },
    "design_review": {
        # "window" and "exterior" alone were removed — as bare words they
        # matched incidental mentions in completely unrelated bills (e.g. a
        # codification bill got flagged priority-5 here on nothing more
        # than the word "exterior" appearing somewhere in its text). Kept
        # only phrases specific enough to actually indicate an architectural
        # design-matching requirement, the kind SB673/SB840 showed us
        # matters most.
        "keywords": ["design requirement", "architectural", "resemble the principal",
                     "roof pitch", "siding", "window type", "window trim",
                     "trim style", "wall articulation", "exterior of the building",
                     "exterior finish"],
        "weight": 5,
    },
    "traffic_impact": {
        "keywords": ["traffic impact", "traffic study", "traffic operations", "traffic effects"],
        "weight": 4,
    },
    "impact_fee": {
        "keywords": ["impact fee"],
        "weight": 4,
    },
    "parking": {
        "keywords": ["parking space", "covered parking", "off-site parking", "parking structure",
                     "multilevel parking"],
        "weight": 3,
    },
    "density": {
        "keywords": ["units per acre", "density", "dwelling units per"],
        "weight": 3,
    },
    "height": {
        "keywords": ["building height", "maximum height", "height limitation"],
        "weight": 3,
    },
    "setback": {
        "keywords": ["setback", "building plane", "front yard", "rear yard", "side yard"],
        "weight": 3,
    },
    "lot_size": {
        "keywords": ["lot area", "lot size", "square feet", "minimum lot"],
        "weight": 3,
    },
    "utility_extension": {
        "keywords": ["utility facility", "sewer", "water access", "extension, upgrade"],
        "weight": 2,
    },
    "open_space": {
        "keywords": ["open space", "permeable surface", "impervious"],
        "weight": 2,
    },
    "permit_process": {
        "keywords": ["administratively approve", "ministerial", "discretionary approval",
                     "special use permit", "conditional use"],
        "weight": 3,
    },
}

# Numeric extraction: captures a number followed by a unit word/abbreviation.
# Good enough to pull "10 feet", "800 square feet", "36 units per acre" etc.
# for side-by-side display — not meant to auto-compare values, since units
# and denominators (per acre vs per lot) need a human to reconcile.
NUMERIC_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(square feet|sq\.?\s*ft\.?|feet|ft\.?|acres?|units? per acre|percent|%|stories?|bedrooms?)",
    re.IGNORECASE,
)


def extract_numbers(text):
    return [f"{m.group(1)} {m.group(2)}" for m in NUMERIC_RE.finditer(text)]


def detect_categories(text):
    text_lower = text.lower()
    found = []
    for cat, info in CATEGORIES.items():
        if any(kw in text_lower for kw in info["keywords"]):
            found.append(cat)
    return found


# ---------- City code corpus ----------

def load_code_corpus():
    records = []
    if not CODE_DIR.exists():
        raise SystemExit(f"{CODE_DIR} not found — run code_to_json.py first.")

    for path in CODE_DIR.rglob("*.json"):
        rec = json.loads(path.read_text(encoding="utf-8-sig"))
        if "text" in rec:
            rec["search_text"] = f"{rec.get('title', '')}\n{rec['text']}"
            rec["kind"] = "narrative"
        elif "standards" in rec:
            standards_str = " ".join(f"{k}: {v}" for k, v in rec["standards"].items())
            rec["search_text"] = f"{rec.get('zoning_district', '')} {rec.get('permitted_use', '')} {standards_str}"
            rec["kind"] = "table_row"
        else:
            continue
        rec["categories"] = detect_categories(rec["search_text"])
        records.append(rec)

    return records


def build_index(records):
    vectorizer = TfidfVectorizer(stop_words="english", max_features=5000)
    matrix = vectorizer.fit_transform([r["search_text"] for r in records])
    return vectorizer, matrix


def top_candidates(provision_text, bill_cats, vectorizer, matrix, records, k=TOP_K_CANDIDATES):
    """
    Retrieval was previously ranking the ENTIRE corpus by raw TF-IDF
    similarity first, then taking the top K — meaning a genuinely relevant
    code section (e.g. 20.10.035 for an ADU bill) could get cut simply
    because its wording didn't overlap enough with the bill's phrasing,
    even though both cover the same regulatory category. Fixed by
    filtering to category-matching records FIRST, then ranking only that
    subset by similarity. If a provision matched no category at all,
    there's nothing to check it against — return no candidates rather
    than forcing an irrelevant top-K.
    """
    if not bill_cats:
        return []

    eligible_idx = [i for i, r in enumerate(records) if set(r["categories"]) & set(bill_cats)]
    if not eligible_idx:
        return []

    query_vec = vectorizer.transform([provision_text])
    scores = cosine_similarity(query_vec, matrix).flatten()

    eligible_idx.sort(key=lambda i: scores[i], reverse=True)
    top_idx = eligible_idx[:k]
    return [(records[i], float(scores[i])) for i in top_idx]


# ---------- Bill splitting ----------

# Bills that AMEND existing code (rather than adding a whole new chapter,
# like SB15/SB840/SB673 did) are typically structured as "SECTION 1. ...
# is amended by adding Subsection (c) to read as follows: ..." — a coarser
# top-level numbering that never matches BILL_SECTION_RE. This is the
# fallback pattern for that style.
AMENDING_SECTION_RE = re.compile(r"(SECTION\s+\d+\.)")


def split_bill_provisions(bill_text):
    matches = list(BILL_SECTION_RE.finditer(bill_text))

    if not matches:
        # Try the coarser "SECTION 1." style used by bills that amend
        # existing code rather than add a new chapter.
        fallback_matches = list(AMENDING_SECTION_RE.finditer(bill_text))
        if fallback_matches:
            provisions = []
            for i, m in enumerate(fallback_matches):
                start = m.start()
                end = fallback_matches[i + 1].start() if i + 1 < len(fallback_matches) else len(bill_text)
                provisions.append({"heading": m.group(1).strip(), "text": bill_text[start:end].strip()})
            return provisions
        return [{"heading": "(unparsed — check BILL_SECTION_RE)", "text": bill_text[:4000]}]

    provisions = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(bill_text)
        provisions.append({"heading": m.group(1).strip(), "text": bill_text[start:end].strip()})
    return provisions


# ---------- Rule-based flagging ----------

def flag_pair(provision, code_record):
    """
    Returns a flag entry if the bill provision and code section share at
    least one regulatory category, else None. No judgment about whether
    they actually conflict — that's the human's job at this stage.
    """
    bill_cats = detect_categories(provision["text"])
    shared = [c for c in bill_cats if c in code_record["categories"]]
    if not shared:
        return None

    bill_numbers = extract_numbers(provision["text"])
    code_numbers = extract_numbers(code_record["search_text"])

    # Rough priority proxy from category weights — NOT an AI severity
    # judgment, just a sort key so higher-stakes categories (occupancy,
    # design review) surface above things like open-space percentages.
    priority = max(CATEGORIES[c]["weight"] for c in shared)

    # Confidence heuristic: more shared categories + numbers present on
    # both sides = worth looking at sooner; a single category match with
    # no numbers on either side is often a coincidental keyword hit.
    if len(shared) >= 2 or (bill_numbers and code_numbers):
        rule_confidence = "high"
    elif bill_numbers or code_numbers:
        rule_confidence = "medium"
    else:
        rule_confidence = "low"

    return {
        "bill_section": provision["heading"],
        "bill_text_excerpt": provision["text"][:400],
        "bill_numbers_found": bill_numbers,
        "code_section": code_record.get("section") or
            f"{code_record.get('table')} — {code_record.get('zoning_district')} / {code_record.get('permitted_use')}",
        "code_text_excerpt": code_record["search_text"][:400],
        "code_numbers_found": code_numbers,
        "shared_categories": shared,
        "priority": priority,
        "rule_confidence": rule_confidence,
        "needs_manual_review": True,
        "conflict_type": "flagged_for_review",
    }


def score_bill(bill_meta, bill_text, vectorizer, matrix, records):
    provisions = split_bill_provisions(bill_text)
    flags = []

    for provision in provisions:
        bill_cats = detect_categories(provision["text"])
        candidates = top_candidates(provision["text"], bill_cats, vectorizer, matrix, records)
        for code_record, sim_score in candidates:
            flag = flag_pair(provision, code_record)
            if flag:
                flag["retrieval_similarity"] = round(sim_score, 3)
                flags.append(flag)

    priorities = [f["priority"] for f in flags]
    overall_severity = max(priorities) if priorities else None
    # "scope" (uniform vs zone_dependent) genuinely needs a human read of
    # which zoning districts are implicated — left null here rather than
    # guessed, since getting this wrong misleads the memo-writing step.
    scope = "needs_manual_review" if flags else None

    return {
        "bill_id": bill_meta["bill_id"],
        "bill_number": bill_meta["bill_number"],
        "title": bill_meta["title"],
        "lifecycle": bill_meta["lifecycle"],
        "legiscan_status": bill_meta["status"],
        "last_action": bill_meta["last_action"],
        "last_action_date": bill_meta["last_action_date"],
        "overall_severity": overall_severity,
        "scope": scope,
        "conflicts": sorted(flags, key=lambda f: f["priority"], reverse=True),
        "gaps": [],  # rule-based pass can't reliably detect "bill covers something code is silent on" —
                     # that requires understanding absence, not pattern matching. Leave for manual review.
        "scoring_method": "rule_based_prescreen",
    }


# ---------- Bill text loading ----------

def get_bills_needing_review(conn, rescan_all=False):
    query = "SELECT bill_id, bill_number, title, lifecycle, status, last_action, last_action_date FROM bills"
    if not rescan_all:
        query += " WHERE needs_review = 1"
    rows = conn.execute(query).fetchall()
    cols = ["bill_id", "bill_number", "title", "lifecycle", "status", "last_action", "last_action_date"]
    return [dict(zip(cols, r)) for r in rows]


def find_bill_text_path(conn, bill_id):
    row = conn.execute(
        "SELECT text_path FROM bill_versions WHERE bill_id = ? ORDER BY doc_date DESC LIMIT 1",
        (bill_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


def extract_plain_text(path):
    """
    LegiScan serves bill text as PDF for most versions, occasionally HTML.
    Both need real extraction, not a raw byte read. (Two bugs were fixed
    here after tracing SB673's zero-flag result: (1) PDFs were being read
    as raw text instead of properly extracted, and (2) HTML entities like
    &#xA0; were being left as literal text instead of decoded, which broke
    the section-header regex match even after tags were stripped — the
    literal characters "&#xA0;" sitting between "Sec." and the section
    number meant the pattern never lined up.)
    """
    path = Path(path)

    if path.suffix.lower() == ".pdf":
        import pdfplumber
        text_parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n".join(text_parts)

    raw = path.read_text(encoding="utf-8-sig", errors="ignore")
    if path.suffix == ".html":
        import html
        text = re.sub(r"<[^>]+>", " ", raw)   # strip tags
        text = html.unescape(text)            # decode &#xA0;, &nbsp;, &amp;, etc.
        return re.sub(r"\s+", " ", text)
    return raw


def update_manifest(bill_result):
    manifest_path = DATA_DIR / "manifest.json"
    if not manifest_path.exists():
        print("  WARNING: data/manifest.json not found — run legiscan_ingest.py first.")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    for entry in manifest:
        if entry["bill_number"] == bill_result["bill_number"]:
            entry["overall_severity"] = bill_result["overall_severity"]
            entry["scope"] = bill_result["scope"]
            break
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def run(rescan_all=False):
    print("Loading city code corpus...")
    records = load_code_corpus()
    print(f"  {len(records)} code record(s) loaded")
    vectorizer, matrix = build_index(records)

    conn = sqlite3.connect(DB_PATH)
    bills = get_bills_needing_review(conn, rescan_all=rescan_all)
    label = "bill(s) total (--rescan-all)" if rescan_all else "bill(s) flagged needs_review=1"
    print(f"{len(bills)} {label}")

    for bill_meta in bills:
        print(f"\nScreening {bill_meta['bill_number']}: {bill_meta['title'][:70]}")

        text_path = find_bill_text_path(conn, bill_meta["bill_id"])
        if not text_path:
            print("  no bill text on file — skipping")
            continue

        bill_text = extract_plain_text(text_path)
        result = score_bill(bill_meta, bill_text, vectorizer, matrix, records)

        out_path = BILLS_OUT / f"{bill_meta['bill_number']}.json"
        existing = json.loads(out_path.read_text(encoding="utf-8-sig")) if out_path.exists() else {}
        existing.update(result)  # preserves 'progress'/'legiscan_url' written by legiscan_ingest.py
        out_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        update_manifest(result)

        conn.execute("UPDATE bills SET needs_review = 0 WHERE bill_id = ?", (bill_meta["bill_id"],))
        conn.commit()

        print(f"  {len(result['conflicts'])} candidate flag(s) — review these manually before "
              f"drafting any memo language")

    print("\nDone. Zero API calls made. Flagged items need a human read (or a copy-paste "
          "into a regular claude.ai chat) to confirm whether they're real conflicts.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Rule-based bill/code conflict pre-screen")
    parser.add_argument("--rescan-all", action="store_true",
                         help="Re-screen every bill in the tracker, ignoring needs_review "
                              "(use after changing CATEGORIES, regex, or fixing a bug like "
                              "the PDF extraction fix — not needed for a normal weekly run)")
    args = parser.parse_args()
    run(rescan_all=args.rescan_all)