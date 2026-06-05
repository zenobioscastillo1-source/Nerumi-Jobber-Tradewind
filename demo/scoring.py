"""Demo harness for the Score & Prioritize layer (lives in demo/, not a production tool).

Simulates a scoring run over a fake GreenLeaf Landscaping account so the Loom video can show the
"Prioritized Leads" tab fill with RANKED leads (hot -> defer) from one command, and a polite waitlist
reply auto-drafted for the low-priority reachable leads. Uses the REAL tools/ code:
  - tools/scoring_rules.score_lead  (the deterministic 0-100 score + suggested tier)
  - tools/check_draft_grounding.py  (the real Layer 2 grounding gate, for the deferral reply)
  - tools/log_to_sheet.py           (the real idempotent Sheet upsert)
Nothing is sent and nothing touches Jobber. Rows are DEMO/SYNTHETIC-labeled and safe to delete.

Run:  python demo/scoring.py
"""
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from scoring_rules import score_lead              # noqa: E402  (real scoring engine)
from jobber_queries import format_address         # noqa: E402
from google_auth import sheets_service            # noqa: E402
from sheets_io import get_spreadsheet_id, quote_tab  # noqa: E402
from sheet_schema import SCORING_COLUMNS          # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
DEMO_NOTE = "SYNTHETIC DEMO DATA - safe to delete"


def C(cid, first, last, emails, phones, city, street=""):
    addr = {"street1": street, "city": city, "province": "TX", "country": "USA"}
    return {"id": cid, "name": " ".join(x for x in [first, last] if x), "first_name": first,
            "last_name": last, "company_name": "", "emails": emails, "phones": phones,
            "address": addr, "address_str": format_address(addr)}


# --- the fake GreenLeaf account: 8 inbound leads engineered for a clear tier spread ---------------
REQUESTS = [
    {"request_id": "REQ-SCORE-001", "created_at": "2026-06-05T09:00:00Z", "source": "Referral",
     "title": "Full backyard landscape install — needed this week before our event",
     "client": C("CLI-S-1", "Maria", "Gonzalez", ["maria.g@example.com"], ["(512) 555-0101"],
                 "Austin", "412 Pecan St")},
    {"request_id": "REQ-SCORE-002", "created_at": "2026-06-05T09:10:00Z", "source": "Referral",
     "title": "Patio and retaining wall installation",
     "client": C("CLI-S-2", "Daniel", "Brooks", ["dbrooks@example.com"], ["(512) 555-0202"],
                 "Round Rock", "88 Walnut Dr")},
    {"request_id": "REQ-SCORE-003", "created_at": "2026-06-05T09:20:00Z", "source": "Website",
     "title": "Monthly lawn maintenance for our property",
     "client": C("CLI-S-3", "Priya", "Shah", ["priya.shah@example.com"], ["(512) 555-0303"],
                 "Pflugerville", "21 Cedar Ln")},
    {"request_id": "REQ-SCORE-004", "created_at": "2026-06-05T09:30:00Z", "source": "Website",
     "title": "Quote for a small flower bed refresh",
     "client": C("CLI-S-4", "Tom", "Becker", ["tbecker@example.com"], ["(512) 555-0404"],
                 "Cedar Park", "9 Maple Ct")},
    {"request_id": "REQ-SCORE-005", "created_at": "2026-06-05T09:40:00Z", "source": "Google",
     "title": "Sprinkler repair",
     "client": C("CLI-S-5", "Karen", "White", ["kwhite@example.com"], ["(512) 555-0505"],
                 "Austin", "303 Oak Blvd")},
    {"request_id": "REQ-SCORE-006", "created_at": "2026-06-05T09:50:00Z", "source": "Facebook",
     "title": "How much for mowing?",
     "client": C("CLI-S-6", "Greg", "Stone", [], ["(512) 555-0606"], "San Marcos")},
    {"request_id": "REQ-SCORE-007", "created_at": "2026-06-05T10:00:00Z", "source": "Facebook",
     "title": "Need a price",
     "client": C("CLI-S-7", "K.", "Lawson", [], [], "Waco")},
    {"request_id": "REQ-SCORE-008", "created_at": "2026-06-05T10:10:00Z", "source": "Facebook",
     "title": "Looking for a quick mow quote",
     "client": C("CLI-S-8", "Bianca", "Reyes", ["breyes@example.com"], ["(512) 555-0808"], "Kyle")},
]


def _tool(args, retries=4, backoff=20):
    """Run a project tool as a subprocess. The Google Sheets API caps reads at 60/min/user, and each
    upsert does a few bootstrap reads — a burst of rows can trip a 429. Back off and retry on that."""
    for attempt in range(retries):
        r = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=str(ROOT))
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0 or not ("429" in out or "RATE_LIMIT" in out):
            return out, r.returncode
        if attempt < retries - 1:
            print(f"    (Sheets 429 rate-limit — backing off {backoff}s, retry {attempt + 1}/{retries - 1})")
            time.sleep(backoff)
    return out, r.returncode


def _write_tmp(name, payload):
    p = ROOT / ".tmp" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def log_row(target, row):
    """Real idempotent upsert via tools/log_to_sheet.py. Returns the verb (APPENDED/SKIPPED/...)."""
    p = _write_tmp(f"_scoring_row_{target}.json", row)
    out, _ = _tool([str(TOOLS / "log_to_sheet.py"), "--target", target, "--row-file", str(p)])
    return out.split()[0] if out else "?"


def deferral_draft(req):
    """Compose a clearly-templated waitlist reply, run it through the REAL grounding gate, then log it."""
    c = req["client"]
    first = c["first_name"] or "there"
    topic = req["title"].rstrip("?.!").lower()
    draft = {
        "request_id": req["request_id"], "client_id": c["id"], "client_name": c["name"],
        "request_title": req["title"], "next_step": "Offer waitlist / follow up when capacity opens",
        "email_subject": "Thanks for reaching out to GreenLeaf",
        "email_body": (f"Hi {first}, thanks so much for reaching out about {topic}! We're flattered you "
                       f"thought of us. We're near capacity right now and like to give every yard our full "
                       f"attention, so we're keeping a short waitlist — may we add you and reach out the "
                       f"moment an opening comes up? — The GreenLeaf team"),
        "sms_body": (f"Hi {first}! Thanks for reaching out to GreenLeaf. We're near capacity right now — "
                     f"may we add you to our waitlist and follow up as soon as we have an opening? — GreenLeaf"),
        "draft_grounding": "voice.md",        # tone only; a courtesy note makes no factual claim
        "ungrounded_flags": "",
        "status": "drafted", "owner_edit": "", "drafted_at": NOW, "owner_notes": DEMO_NOTE,
    }
    draft_path = _write_tmp("_scoring_draft_in.json", draft)
    emit_path = ROOT / ".tmp" / "_scoring_draft_out.json"
    out, code = _tool([str(TOOLS / "check_draft_grounding.py"), "--draft-file", str(draft_path),
                       "--emit-row", str(emit_path)])
    if code != 0:
        return f"GATE-FAIL ({out.splitlines()[0]})"
    emitted = json.loads(emit_path.read_text(encoding="utf-8"))
    log_out, _ = _tool([str(TOOLS / "log_to_sheet.py"), "--target", "drafts", "--row-file", str(emit_path)])
    return log_out.split()[0] if log_out else "?"


def run_pass(label):
    print(f"\n===== {label} =====")
    scored = []
    for req in REQUESTS:
        s = score_lead(req, req["client"])
        scored.append((req, s))
    scored.sort(key=lambda rs: rs[1]["score"], reverse=True)   # rank: highest first

    for req, s in scored:
        c = req["client"]
        tier = s["suggested_tier"]
        reachable = s["signals"]["contactability"]["reachable"]
        will_defer = (tier == "defer" and reachable)           # can only reply to reachable leads
        row = {
            "request_id": req["request_id"], "created_at": req["created_at"],
            "request_title": req["title"], "request_source": req["source"],
            "client_id": c["id"], "client_name": c["name"],
            "score": s["score"], "priority_tier": tier,
            "priority_reason": "; ".join(s["reasons"][:3]),
            "top_signals": s["top_signals"], "recommended_action": s["recommended_action"],
            "deferral_drafted": "yes" if will_defer else ("n/a - unreachable" if tier == "defer" else "no"),
            "status": "scored", "surfaced_at": NOW, "owner_notes": DEMO_NOTE,
        }
        res = log_row("scoring", row)
        draft_res = deferral_draft(req) if will_defer else "-"
        print(f"  {req['request_id']}  score={s['score']:>3}  {tier:<5}  {c['name']:<16}"
              f" | leads:{res:<9} defer-draft:{draft_res}")


def dump_tab(svc, sid, tab, cols, show):
    rows = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"{quote_tab(tab)}!A1:{chr(64+len(cols))}").execute().get("values", [])
    print(f"\n----- {tab}: {max(0, len(rows)-1)} data row(s) -----")
    idx = [cols.index(s) for s in show]
    for r in rows[1:]:
        r = r + [""] * (len(cols) - len(r))
        print("   " + " | ".join(str(r[i]) for i in idx))


def main():
    run_pass("PASS 1 (first surfacing — ranked)")
    run_pass("PASS 2 (re-run -> expect SKIPPED, dedup)")
    svc, sid = sheets_service(), get_spreadsheet_id()
    dump_tab(svc, sid, "Prioritized Leads", SCORING_COLUMNS,
             ["score", "priority_tier", "client_name", "request_title", "recommended_action", "deferral_drafted"])


if __name__ == "__main__":
    main()
