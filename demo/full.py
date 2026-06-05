"""One-command demo of the whole pipeline (lives in demo/, writes to the real Google Sheet).

Runs the read-only Jobber engine end to end against the unified fake "GreenLeaf Landscaping"
account, so a single command fills every tab:

  Step 1  reset             clear all five demo tabs, keep headers          [reset.reset_all]
  Step 2  ingest + hygiene  surface Requests, flag duplicates/incomplete    [simulation.run_pass]
  Step 3  score + prioritize rank each lead 0-100 -> hot/warm/cool/defer    [scoring_rules.score_lead]
  Step 4  follow-up drafts  hot -> personalized reply; defer -> waitlist    [check_draft_grounding gate]
  Step 5  cleanup proposal  merge proposal for the strong duplicate         [propose_cleanup.propose]

Reuses the REAL tools/ code (detection, scoring, the grounding gate, the idempotent Sheet upsert)
and the sibling demo datasets (simulation.py, scoring.py) — nothing here re-implements that logic.
Nothing touches Jobber and nothing is sent; every row is SYNTHETIC/DEMO-labeled and safe to delete
(just re-run to refresh). A demo harness, not a production entrypoint.

A short sleep paces the Sheet writes under the ~60-req/min API quota; the upsert path also retries
on a 429 (inherited from scoring.py), so a burst backs off instead of failing.

Run:  python demo/full.py
"""
import datetime
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
(ROOT / ".tmp").mkdir(parents=True, exist_ok=True)   # disposable scratch dir (gitignored) for intermediates
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT / "demo"))   # import the sibling demo modules

from dotenv import load_dotenv                                          # noqa: E402
import reset                                                            # noqa: E402
import simulation as sim    # unified GreenLeaf hygiene dataset + detection; stubs F._search  # noqa: E402
import scoring as sco       # scoring dataset + waitlist draft + retry-aware Sheet I/O          # noqa: E402
import find_duplicate_clients as F                                      # noqa: E402
from scoring_rules import score_lead                                    # noqa: E402  (real scorer)
from propose_cleanup import propose                                     # noqa: E402  (real proposer)
from google_auth import sheets_service                                  # noqa: E402
from sheets_io import get_spreadsheet_id, quote_tab                     # noqa: E402
from sheet_schema import TARGETS                                        # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
DEMO_NOTE = "SYNTHETIC DEMO DATA - safe to delete"
SLEEP = 2.0   # pause between Sheet writes to stay under the 60/min quota


def log(target, row):
    """Idempotent Sheet upsert via the REAL tool (retry-aware), then pace for the quota."""
    verb = sco.log_row(target, row)   # reuse scoring.py's 429-retry wrapper around log_to_sheet.py
    time.sleep(SLEEP)
    return verb


# Route simulation.py's writes through the retry-aware, paced upsert so Step 2 reuses its whole
# run_pass (row building + hygiene detection) without its un-paced writer.
sim.upsert = lambda target, row: log(target, row)


def hot_draft(req):
    """Compose a personalized reply for a HOT lead, run it through the REAL grounding gate, log it.

    Twin of scoring.deferral_draft (same tool plumbing). Claims are limited to a free,
    no-obligation on-site estimate (pricing.md) in the owner's voice (voice.md) — no service or
    price promise — so it passes the gate without inventing anything."""
    c = req["client"]
    first = c["first_name"] or "there"
    draft = {
        "request_id": req["request_id"], "client_id": c["id"], "client_name": c["name"],
        "request_title": req["title"],
        "next_step": "Offer a free on-site estimate and confirm the best time to visit",
        "email_subject": "Thanks for reaching out to GreenLeaf",
        "email_body": (f"Hi {first}, thanks so much for reaching out to GreenLeaf! We'd love to learn more "
                       f"about your project and see how we can help. Our on-site estimates are free and "
                       f"no-obligation, and we can usually get out within a couple of business days. What day "
                       f"and time work best for you? — The GreenLeaf team"),
        "sms_body": (f"Hi {first}! Thanks for reaching out to GreenLeaf. We'd love to help with your project — "
                     f"our on-site estimate is free and we can usually come out within a couple of business "
                     f"days. What time works best? — GreenLeaf"),
        "draft_grounding": "voice.md; pricing.md",   # tone (voice) + free no-obligation estimate (pricing)
        "ungrounded_flags": "",
        "status": "drafted", "owner_edit": "", "drafted_at": NOW, "owner_notes": DEMO_NOTE,
    }
    draft_path = sco._write_tmp("_hot_draft_in.json", draft)
    emit_path = ROOT / ".tmp" / "_hot_draft_out.json"
    out, code = sco._tool([str(TOOLS / "check_draft_grounding.py"), "--draft-file", str(draft_path),
                           "--emit-row", str(emit_path)])
    if code != 0:
        return f"GATE-FAIL ({out.splitlines()[0]})"
    log_out, _ = sco._tool([str(TOOLS / "log_to_sheet.py"), "--target", "drafts", "--row-file", str(emit_path)])
    return log_out.split()[0] if log_out else "?"


def step_2_ingest_hygiene():
    print("\n========== STEP 2: Layer 1 ingest + hygiene detection ==========")
    sim.run_pass("ingesting GreenLeaf Requests + flagging duplicates / incomplete")


def step_3_score(scored):
    print("\n========== STEP 3: score + prioritize leads ==========")
    for req, s in scored:
        c = req["client"]
        tier = s["suggested_tier"]
        reachable = s["signals"]["contactability"]["reachable"]
        will_defer = (tier == "defer" and reachable)
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
        verb = log("scoring", row)
        print(f"  {req['request_id']}  score={s['score']:>3}  {tier:<5}  {c['name']:<16} | leads:{verb}")


def step_4_drafts(scored):
    print("\n========== STEP 4: generate follow-up drafts ==========")
    print("  (hot -> personalized reply; defer + reachable -> warm waitlist; both pass the grounding gate)")
    for req, s in scored:
        c = req["client"]
        tier = s["suggested_tier"]
        reachable = s["signals"]["contactability"]["reachable"]
        if tier == "hot":
            res, kind = hot_draft(req), "personalized reply"
            time.sleep(SLEEP)
        elif tier == "defer" and reachable:
            res, kind = sco.deferral_draft(req), "warm waitlist"
            time.sleep(SLEEP)
        elif tier == "defer":
            res, kind = "-", "no draft (unreachable)"
        else:
            res, kind = "-", "no draft (warm/cool)"
        print(f"  {req['request_id']}  {tier:<5}  {c['name']:<16}  {kind:<20} draft:{res}")


def step_5_proposal():
    print("\n========== STEP 5: cleanup proposal for the strong duplicate ==========")
    # The strong-duplicate pair (shared phone) lives in the fixture account; find it via the real
    # duplicate finder (its search is stubbed to that account by the simulation import).
    strong_client, matches = None, None
    for client in sim.ACCOUNT:
        m = F.find_duplicates(client, 10)
        if m["signal"] == "strong":
            strong_client, matches = client, m
            break
    if not strong_client:
        print("  (no strong duplicate found — nothing to propose)")
        return
    result = propose(strong_client, matches)
    proposal = result.get("proposal")
    if not proposal:
        print(f"  (no merge proposal produced: decision={result['decision']})")
        return
    proposal["owner_notes"] = DEMO_NOTE
    print(f"  subject {strong_client['id']} ({strong_client['name']}): "
          f"{result['decision']} -> {proposal['proposal_id']}")
    print(f"    change: {proposal['proposed_change']}")
    print(f"    proposals: {log('proposals', proposal)}")


def verify(svc, sid):
    print("\n========== VERIFY: data rows now in each tab ==========")
    for target in reset.RESET_TARGETS:
        cfg = TARGETS[target]
        tab = os.environ.get(cfg["tab_env"], cfg["tab_default"]).strip()
        rows = svc.spreadsheets().values().get(
            spreadsheetId=sid, range=f"{quote_tab(tab)}!A2:A").execute().get("values", [])
        n = len([r for r in rows if r and r[0].strip()])
        print(f"  {tab:<18} {n:>2} data row(s)")


def main():
    load_dotenv()
    svc, sid = sheets_service(), get_spreadsheet_id()

    print("========== STEP 1: reset all demo tabs ==========")
    reset.reset_all(svc, sid)

    step_2_ingest_hygiene()

    # Score once; reuse the ranked list for both the scoring tab (Step 3) and the drafts (Step 4).
    scored = sorted(((req, score_lead(req, req["client"])) for req in sco.REQUESTS),
                    key=lambda rs: rs[1]["score"], reverse=True)
    step_3_score(scored)
    step_4_drafts(scored)
    step_5_proposal()

    verify(svc, sid)
    print("\nDone. All five tabs reflect one unified GreenLeaf Landscaping demo run.")


if __name__ == "__main__":
    main()
