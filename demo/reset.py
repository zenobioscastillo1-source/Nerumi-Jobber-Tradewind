"""Demo helper (lives in demo/, not a production tool).

Clears every DATA row (row 2 onward) from the five customer-facing demo tabs, leaving each
HEADER row (row 1) intact, so a demo run starts from a clean slate. Reversible Sheet writes
only — no Jobber, no network sends (CLAUDE.md: clearing/writing the tracking Sheet is autonomous).

It deliberately does NOT touch the `_state` tab: that holds the durable last-seen cursor, and
wiping it would make Layer 1 re-process or skip Requests. Only the five surfaced/output tabs are
cleared; tab names are resolved from sheet_schema.TARGETS (env-aware), so they never drift.

Importable: full.py calls reset_all(svc, sid). Runnable standalone: python demo/reset.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from dotenv import load_dotenv                                          # noqa: E402
from google_auth import sheets_service                                 # noqa: E402
from sheets_io import get_spreadsheet_id, quote_tab, tab_exists        # noqa: E402
from sheet_schema import TARGETS                                       # noqa: E402

# The five customer-facing tabs to wipe (NOT `_state`, which is durable control-state).
RESET_TARGETS = ["requests", "hygiene", "scoring", "drafts", "proposals"]


def _tab_name(target: str) -> str:
    cfg = TARGETS[target]
    return os.environ.get(cfg["tab_env"], cfg["tab_default"]).strip()


def reset_all(svc=None, sid=None):
    """Clear row 2+ of each demo tab; keep the header. Returns (svc, sid) for connection reuse."""
    load_dotenv()
    svc = svc or sheets_service()
    sid = sid or get_spreadsheet_id()
    for target in RESET_TARGETS:
        tab = _tab_name(target)
        if not tab_exists(svc, sid, tab):
            print(f"  - {tab!r}: not present, skipped (nothing to clear)")
            continue
        existing = svc.spreadsheets().values().get(
            spreadsheetId=sid, range=f"{quote_tab(tab)}!A2:A").execute().get("values", [])
        n = len([r for r in existing if r and r[0].strip()])
        svc.spreadsheets().values().clear(
            spreadsheetId=sid, range=f"{quote_tab(tab)}!A2:Z").execute()
        print(f"  - {tab!r}: cleared {n} data row(s), header kept")
        time.sleep(0.5)  # gentle pacing under the Sheets API quota
    return svc, sid


def main():
    print("Resetting demo tabs (clearing row 2 onward, keeping headers; _state left intact)...")
    reset_all()
    print("Reset complete.")


if __name__ == "__main__":
    main()
