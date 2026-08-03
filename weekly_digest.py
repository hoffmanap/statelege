"""
Weekly digest generator — El Paso Legislative Watch
90th Texas Legislature prep

Reads every data/bills/{bill_number}.json file and produces a single
scannable digest (Markdown) of what actually needs attention this week —
instead of clicking into each bill individually. Run this AFTER
prescreen_conflicts.py (or score_conflicts.py, if you switch back to the
LLM version later — this reads the same output schema either way).

NO API CALL. NO COST. Pure aggregation over files you already have.

Usage:
    python weekly_digest.py

Output:
    data/weekly_digest.md   — human-readable summary
    data/weekly_digest.json — same content, structured, in case you want
                              to pipe it into another tool later
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
BILLS_DIR = DATA_DIR / "bills"

# Only include bills whose priority/severity meets this floor in the
# "needs attention" section. Lower-priority flags still get counted but
# don't clutter the top of the digest. Adjust as you calibrate against
# real weeks — 3 was chosen because that's where SB15-style numeric
# conflicts land, one notch below owner-occupancy/design-review hits.
PRIORITY_FLOOR = 3


def load_all_bills():
    bills = []
    if not BILLS_DIR.exists():
        raise SystemExit(f"{BILLS_DIR} not found — run legiscan_ingest.py and "
                          f"prescreen_conflicts.py first.")
    for path in sorted(BILLS_DIR.glob("*.json")):
        try:
            bills.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            print(f"  WARNING: could not parse {path.name}, skipping")
    return bills


def summarize(bills):
    flagged = [b for b in bills if b.get("conflicts")]
    never_scored = [b for b in bills if not b.get("scoring_method")]

    high_priority = [b for b in flagged if (b.get("overall_severity") or 0) >= PRIORITY_FLOOR]
    high_priority.sort(key=lambda b: b.get("overall_severity") or 0, reverse=True)

    lifecycle_counts = {"active": 0, "passed": 0, "dead": 0}
    for b in bills:
        lc = b.get("lifecycle")
        if lc in lifecycle_counts:
            lifecycle_counts[lc] += 1

    return {
        "total_bills_tracked": len(bills),
        "lifecycle_counts": lifecycle_counts,
        "flagged_count": len(flagged),
        "high_priority_count": len(high_priority),
        "never_scored_count": len(never_scored),
        "high_priority_bills": high_priority,
        "all_flagged_bills": flagged,
    }


def render_markdown(summary, generated_at):
    lines = []
    lines.append("# Legislative Watch — Weekly Digest")
    lines.append(f"_Generated {generated_at}_\n")

    lc = summary["lifecycle_counts"]
    lines.append(f"**{summary['total_bills_tracked']} bills tracked** — "
                  f"{lc['active']} active, {lc['passed']} passed, {lc['dead']} dead\n")
    lines.append(f"**{summary['flagged_count']} bill(s)** have at least one candidate "
                  f"conflict flag against the current code corpus. "
                  f"**{summary['high_priority_count']}** are priority {PRIORITY_FLOOR}+ "
                  f"and worth reading first.\n")

    if summary["never_scored_count"]:
        lines.append(f"_Note: {summary['never_scored_count']} bill(s) in the tracker have not "
                      f"been scored yet — run prescreen_conflicts.py to cover them._\n")

    lines.append("---\n")
    lines.append("## Priority bills — read these first\n")

    if not summary["high_priority_bills"]:
        lines.append("_None this week._\n")
    else:
        for b in summary["high_priority_bills"]:
            lines.append(f"### {b['bill_number']} — {b['title']}")
            lines.append(f"- **Lifecycle:** {b.get('lifecycle', 'unknown')} · "
                          f"**Overall priority:** {b.get('overall_severity')} · "
                          f"**Last action:** {b.get('last_action', '—')} "
                          f"({b.get('last_action_date', '—')})")
            if b.get("legiscan_url"):
                lines.append(f"- [View on LegiScan]({b['legiscan_url']})")
            lines.append("")
            for c in sorted(b["conflicts"], key=lambda x: x.get("priority", 0), reverse=True)[:5]:
                cats = ", ".join(c.get("shared_categories", []))
                lines.append(f"  - **{c['bill_section']}** &rarr; **{c['code_section']}** "
                              f"_(priority {c.get('priority')}, {c.get('rule_confidence')} confidence, "
                              f"categories: {cats})_")
            if len(b["conflicts"]) > 5:
                lines.append(f"  - _...and {len(b['conflicts']) - 5} more flag(s), see bill.html for full list_")
            lines.append("")

    lines.append("---\n")
    lines.append("## All flagged bills (lower priority included)\n")

    if not summary["all_flagged_bills"]:
        lines.append("_No flags this week._\n")
    else:
        lines.append("| Bill | Title | Lifecycle | Priority | Flags |")
        lines.append("|---|---|---|---|---|")
        for b in sorted(summary["all_flagged_bills"],
                         key=lambda x: x.get("overall_severity") or 0, reverse=True):
            title_short = (b["title"][:60] + "…") if len(b["title"]) > 60 else b["title"]
            lines.append(f"| {b['bill_number']} | {title_short} | {b.get('lifecycle','—')} | "
                          f"{b.get('overall_severity','—')} | {len(b['conflicts'])} |")

    lines.append("\n---\n")
    lines.append("_This digest is generated from rule-based keyword/pattern matching, not legal "
                  "or AI-verified analysis. Every flagged item needs a human read before it goes "
                  "into any memo. City Attorney confirmation is required before any code amendment._")

    return "\n".join(lines)


def run():
    bills = load_all_bills()
    summary = summarize(bills)
    generated_at = datetime.now(timezone.utc).isoformat()

    markdown = render_markdown(summary, generated_at)
    (DATA_DIR / "weekly_digest.md").write_text(markdown)

    json_out = {
        "generated_at": generated_at,
        "total_bills_tracked": summary["total_bills_tracked"],
        "lifecycle_counts": summary["lifecycle_counts"],
        "flagged_count": summary["flagged_count"],
        "high_priority_count": summary["high_priority_count"],
        "high_priority_bill_numbers": [b["bill_number"] for b in summary["high_priority_bills"]],
    }
    (DATA_DIR / "weekly_digest.json").write_text(json.dumps(json_out, indent=2))

    print("Wrote data/weekly_digest.md and data/weekly_digest.json")
    print(f"{summary['flagged_count']} bill(s) flagged, "
          f"{summary['high_priority_count']} at priority {PRIORITY_FLOOR}+")


if __name__ == "__main__":
    run()
