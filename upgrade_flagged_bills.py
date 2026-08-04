"""
Upgrade flagged bills with real LLM analysis — El Paso Legislative Watch

Takes the candidate pairs already found for free by prescreen_conflicts.py
(category overlap + retrieval — no API cost) and sends them to Claude for
actual judgment: is this a real conflict, how severe, what's the specific
required change. This is a SECOND, OPTIONAL pass on top of the free
pre-screen — it does not replace it and does not redo retrieval.

COST CONTROL:
  - One API call PER BILL, not per candidate pair — all of a bill's
    flagged pairs go in a single request, Claude returns a verdict for
    each. This is what keeps cost low even for bills with 20+ pairs.
  - --min-priority filters out low-value pairs before sending (default 3),
    since low-confidence/low-priority pairs are usually noise, not worth
    paying to verify.
  - --bill lets you test on exactly one bill first.
  - --limit caps how many bills get processed in one run, so you can do
    a small paid test before committing to the full set.
  - Bills already upgraded (marked "llm_verified": true) are skipped on
    subsequent runs unless --force is passed, so re-running doesn't
    re-charge for bills already done.

Usage:
    pip install anthropic --break-system-packages
    export ANTHROPIC_API_KEY="your_key_here"

    # Test on one bill first:
    python upgrade_flagged_bills.py --bill HB4695

    # Small paid test, 5 bills:
    python upgrade_flagged_bills.py --limit 5

    # Full run over everything currently flagged:
    python upgrade_flagged_bills.py
"""

import argparse
import json
import re
from pathlib import Path

from anthropic import Anthropic

BILLS_DIR = Path(__file__).parent / "docs" / "data" / "bills"
MANIFEST_PATH = Path(__file__).parent / "docs" / "data" / "manifest.json"

client = Anthropic()

SYSTEM_PROMPT = """You are assisting the City of El Paso Development Services department in \
reviewing pending Texas legislation for conflicts with the city's zoning, subdivision, and building \
code. You are given a bill and a list of candidate (bill provision, city code section) pairs that a \
free keyword-matching pass already identified as POSSIBLY related. Your job is to give a real verdict \
on each pair — the keyword match only tells you they mention similar topics, not whether they actually \
conflict.

Respond with ONLY a JSON array, one object per pair IN THE SAME ORDER given, no preamble, no markdown fences:
[
  {
    "relevant": true or false,
    "conflict_type": "direct_conflict" | "likely_conflict" | "gap" | "no_conflict",
    "severity": integer 1-5 (only meaningful if conflict_type is direct_conflict or likely_conflict),
    "scope": "uniform" | "zone_dependent" | "not_applicable",
    "confidence": "high" | "medium" | "low",
    "required_change": "one sentence describing what city code would need to change, or empty string if no_conflict",
    "affected_zones": ["R-3", "R-4"] if scope is zone_dependent, otherwise null
  },
  ...
]

"relevant": false means the keyword match was a false positive — set conflict_type to "no_conflict".
Severity 5 means the bill flatly prohibits a core existing requirement. Severity 1 means a minor \
technical wrinkle. Be conservative about "zone_dependent" — only use it when the conflict genuinely \
depends on which zoning district a property is in."""


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8-sig"))


def save_manifest(manifest):
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# Bills that got flagged purely on incidental keyword overlap (e.g. a
# manufactured-home TAX bill matching "lot_size") rarely turn into real
# conflicts worth a human's time, even though verifying them costs very
# little. This is an optional relevance pre-filter on the bill TITLE —
# skip bills that don't even mention a housing/zoning/development topic,
# regardless of what category the prescreen matched on internally.
RELEVANCE_KEYWORDS = [
    "zoning", "subdivision", "plat", "residential", "housing", "dwelling",
    "multifamily", "mixed-use", "building code", "permit", "setback",
    "manufactured home", "accessory dwelling", "municipal regulation",
    "land use", "development",
]


def title_is_relevant(title):
    title_lower = title.lower()
    return any(kw in title_lower for kw in RELEVANCE_KEYWORDS)


def get_flagged_bill_numbers(manifest, only_bill=None, require_relevant_title=False):
    if only_bill:
        return [only_bill]
    candidates = [b for b in manifest if b.get("overall_severity") is not None]
    if require_relevant_title:
        candidates = [b for b in candidates if title_is_relevant(b.get("title", ""))]
    return [b["bill_number"] for b in candidates]


BATCH_SIZE = 8  # pairs per API call — keeps responses well within token limits
                 # even for bills with 20-30+ candidate pairs, instead of one
                 # giant call per bill that can get cut off before finishing


def call_llm_for_batch(bill_number, title, pairs):
    pair_descriptions = []
    for p in pairs:
        pair_descriptions.append(
            f"BILL: {p['bill_section']}\n{p.get('bill_text_excerpt', '')[:600]}\n\n"
            f"CODE: {p['code_section']}\n{p.get('code_text_excerpt', '')[:600]}"
        )

    user_prompt = (
        f"BILL: {bill_number} — {title}\n\n"
        f"Evaluate these {len(pairs)} candidate pairs:\n\n"
        + "\n\n---\n\n".join(f"PAIR {i+1}:\n{d}" for i, d in enumerate(pair_descriptions))
    )

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=600 * len(pairs) + 500,  # generous per-pair budget, was too tight before
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        return None, "no text content in response"
    raw = text_blocks[0].strip()
    raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()

    try:
        verdicts = json.loads(raw)
    except json.JSONDecodeError:
        return None, "could not parse LLM response"

    if len(verdicts) != len(pairs):
        return None, f"verdict count ({len(verdicts)}) doesn't match pair count ({len(pairs)})"

    if not all(isinstance(v, dict) for v in verdicts):
        return None, "response contained a malformed (non-object) verdict entry"

    return verdicts, None


def upgrade_bill(bill_number, min_priority):
    path = BILLS_DIR / f"{bill_number}.json"
    if not path.exists():
        print(f"  {bill_number}: no file found, skipping")
        return None

    bill = json.loads(path.read_text(encoding="utf-8-sig"))

    if bill.get("llm_verified") and not upgrade_bill.force:
        print(f"  {bill_number}: already upgraded, skipping (use --force to redo)")
        return None

    pairs = [c for c in bill.get("conflicts", []) if (c.get("priority") or 0) >= min_priority]
    if not pairs:
        print(f"  {bill_number}: no pairs at priority {min_priority}+, skipping")
        return None

    # Process in small batches so large bills (20-30+ pairs) don't produce a
    # response too long to complete, which was silently failing entire bills
    # before regardless of how many pairs would have otherwise worked fine.
    upgraded_conflicts = []
    failed_batches = 0
    for i in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[i:i + BATCH_SIZE]
        verdicts, error = call_llm_for_batch(bill_number, bill.get("title", ""), batch)

        if error:
            print(f"  {bill_number}: batch {i // BATCH_SIZE + 1} failed ({error}), "
                  f"{len(batch)} pair(s) left unupgraded")
            failed_batches += 1
            upgraded_conflicts.extend(batch)  # keep original unupgraded pairs rather than lose them
            continue

        for pair, verdict in zip(batch, verdicts):
            merged = dict(pair)
            merged.update({
                "conflict_type": verdict.get("conflict_type", pair.get("conflict_type")),
                "severity": verdict.get("severity"),
                "scope": verdict.get("scope"),
                "confidence": verdict.get("confidence"),
                "required_change": verdict.get("required_change", ""),
                "affected_zones": verdict.get("affected_zones"),
                "llm_relevant": verdict.get("relevant", True),
            })
            upgraded_conflicts.append(merged)

    # Keep low-priority pairs that weren't sent to the LLM, unchanged
    untouched = [c for c in bill.get("conflicts", []) if (c.get("priority") or 0) < min_priority]

    real_conflicts = [c for c in upgraded_conflicts if c.get("llm_relevant")]
    severities = [c["severity"] for c in real_conflicts if c.get("severity")]

    bill["conflicts"] = upgraded_conflicts + untouched
    prior_severity = bill.get("overall_severity")
    new_severity = max(severities) if severities else None
    bill["overall_severity"] = new_severity if new_severity is not None else prior_severity
    bill["scope"] = (
        "zone_dependent" if any(c.get("scope") == "zone_dependent" for c in real_conflicts)
        else ("uniform" if real_conflicts else bill.get("scope"))
    )
    bill["llm_verified"] = (failed_batches == 0)  # only mark fully verified if every batch succeeded

    path.write_text(json.dumps(bill, indent=2), encoding="utf-8")

    if new_severity is not None:
        print(f"  {bill_number}: upgraded {len(pairs)} pair(s), "
              f"{len(real_conflicts)} confirmed real, new severity {new_severity}"
              f"{' (some batches failed, will retry next run)' if failed_batches else ''}")
    else:
        print(f"  {bill_number}: upgraded {len(pairs)} pair(s), 0 confirmed real by LLM — "
              f"kept prior severity ({prior_severity}) from free pre-screen, no new conflict found"
              f"{' (some batches failed, will retry next run)' if failed_batches else ''}")

    return bill["overall_severity"], bill["scope"]


def run(only_bill=None, limit=None, min_priority=3, force=False, require_relevant_title=False):
    upgrade_bill.force = force  # simple way to pass force flag into the function above

    manifest = load_manifest()
    bill_numbers = get_flagged_bill_numbers(manifest, only_bill=only_bill,
                                             require_relevant_title=require_relevant_title)

    if limit:
        bill_numbers = bill_numbers[:limit]

    print(f"Upgrading {len(bill_numbers)} bill(s), min priority {min_priority}, "
          f"force={force}")

    updated = 0
    for bill_number in bill_numbers:
        result = upgrade_bill(bill_number, min_priority)
        if result:
            severity, scope = result
            for entry in manifest:
                if entry["bill_number"] == bill_number:
                    entry["overall_severity"] = severity
                    entry["scope"] = scope
                    break
            updated += 1

    save_manifest(manifest)
    print(f"\nDone. {updated} bill(s) upgraded with real LLM analysis.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upgrade flagged bills with real LLM analysis")
    parser.add_argument("--bill", help="Upgrade only this bill number (e.g. HB4695)")
    parser.add_argument("--limit", type=int, help="Only process this many bills (for a small paid test)")
    parser.add_argument("--min-priority", type=int, default=3,
                         help="Only send pairs at this priority or higher to the LLM (default 3)")
    parser.add_argument("--force", action="store_true",
                         help="Re-upgrade bills even if already marked llm_verified")
    parser.add_argument("--relevant-titles-only", action="store_true",
                         help="Skip bills whose title doesn't mention a housing/zoning/development "
                              "keyword, even if they were flagged on incidental category overlap")
    args = parser.parse_args()
    run(only_bill=args.bill, limit=args.limit, min_priority=args.min_priority, force=args.force,
        require_relevant_title=args.relevant_titles_only)