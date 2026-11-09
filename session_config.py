"""
Shared session config for Legislative Watch.

One place that knows which Texas legislatures the tracker covers, where each
session's data lives (docs/data/<id>/), and which one is live.

  ACTIVE_SESSION  -> the only session legiscan_ingest.py pulls from LegiScan.
  Older sessions  -> frozen archives; their data folders are never rewritten.

When the 91st convenes (Jan 2029): add a "91" entry, set ACTIVE_SESSION = "91".
"""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DOCS_DATA = ROOT / "docs" / "data"

SESSIONS = {
    "90": {
        "label": "90th Legislature (2027)",
        "year": 2027,
        "start": datetime(2027, 1, 12, tzinfo=timezone.utc),
        "end": datetime(2027, 6, 1, tzinfo=timezone.utc),   # ~sine die + buffer
    },
    "89": {
        "label": "89th Legislature (2025)",
        "year": 2025,
        "start": datetime(2025, 1, 14, tzinfo=timezone.utc),
        "end": datetime(2025, 6, 2, tzinfo=timezone.utc),
    },
}
ACTIVE_SESSION = "90"


def data_dir(session):
    return DOCS_DATA / session


def add_session_arg(parser):
    parser.add_argument("--session", default=ACTIVE_SESSION, choices=sorted(SESSIONS),
                        help=f"Which legislature to operate on (default {ACTIVE_SESSION})")


def write_sessions_index():
    """docs/data/sessions.json — what the front-end toggle reads."""
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    out = {
        "default": ACTIVE_SESSION,
        "sessions": [
            {"id": sid, "label": s["label"], "year": s["year"]}
            for sid, s in sorted(SESSIONS.items(), key=lambda kv: kv[0], reverse=True)
        ],
    }
    (DOCS_DATA / "sessions.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
