# Demo — GreenLeaf Landscaping

A self-contained, runnable demo of the read-only Jobber engine. It exercises the **real**
`tools/` code (detection, scoring, the grounding gate, the idempotent Sheet upsert) against a
fake **GreenLeaf Landscaping** account and surfaces everything to the configured Google Sheet —
so the whole pipeline can be shown without a live Jobber client.

> **Safe by construction.** Nothing here touches Jobber and nothing is sent. Every row is written
> with `owner_notes = "SYNTHETIC DEMO DATA - safe to delete"`. All `knowledge/` facts are
> `status: sample` (GreenLeaf is not a real business), so any draft built on them is flagged
> `sample_grounded` and could never be sent as if it were real.

## One command

```bash
python demo/full.py
```

This runs the full pipeline in five steps and prints progress as it goes:

| Step | What it does | Reuses |
|------|--------------|--------|
| 1 · reset | Clears row 2+ of all five demo tabs (headers kept; `_state` left intact) | `reset.reset_all` |
| 2 · ingest + hygiene | Surfaces Requests, flags **duplicate** + **incomplete** clients | `simulation.run_pass` → `hygiene_rules`, `find_duplicate_clients` |
| 3 · score + prioritize | Ranks each lead 0–100 into **hot / warm / cool / defer** | `scoring_rules.score_lead` |
| 4 · follow-up drafts | **hot** → personalized reply; **defer + reachable** → warm waitlist (both pass the grounding gate) | `tools/check_draft_grounding.py` |
| 5 · cleanup proposal | A `merge_duplicates` proposal for the strong-duplicate pair | `propose_cleanup.propose` |

It finishes by reading each tab back and printing the row counts, so the run self-verifies.

## Prerequisites

Same as the engine itself (see the repo [README](../README.md) and [`.env.example`](../.env.example)):

- `pip install -r requirements.txt`
- `credentials.json` + `token.json` in the repo root (Google OAuth, Sheets scope only)
- `.env` with `SPREADSHEET_ID` set (tab names default to the five below)

The five tabs: **Requests**, **Client Hygiene**, **Prioritized Leads**, **Follow-up Drafts**,
**Cleanup Proposals**. They're created automatically on first write.

## Files

- **`full.py`** — the one-command orchestrator above.
- **`reset.py`** — clears the five demo tabs (importable `reset_all`, or run standalone).
- **`simulation.py`** — the GreenLeaf hygiene dataset (duplicate + incomplete scenarios) and the
  Layer 1 ingest pass. Stubs only the live Jobber `clients` search to read the fixture account.
- **`scoring.py`** — the GreenLeaf scoring dataset (8 leads engineered for a clean tier spread) and
  the waitlist-reply composer. Also runnable standalone.
- **`fixtures/good_draft.json`** — a follow-up draft whose every claim traces to a real `knowledge/`
  entry. **Passes** the grounding gate.
- **`fixtures/fabricated_draft.json`** — a draft with invented claims (flat price, lifetime warranty,
  money-back guarantee) "backed" by knowledge files that don't exist. **Fails** the grounding gate.

```bash
# Demonstrate the fabrication guardrail (first passes, second exits 1):
python tools/check_draft_grounding.py --draft-file demo/fixtures/good_draft.json
python tools/check_draft_grounding.py --draft-file demo/fixtures/fabricated_draft.json
```

## Notes

- Disposable scratch JSON is written to the gitignored `.tmp/` (per `CLAUDE.md`); durable demo code
  lives here in `demo/`. Re-running `full.py` resets and refreshes every tab.
- A `time.sleep` paces the Sheet writes under the Google Sheets ~60-req/min quota; the upsert path
  also backs off and retries on a `429`, so a burst recovers instead of failing.
- **Known limitation shown by the demo:** the scorer measures value/fit, not *can-we-do-it* — a lead
  for a service GreenLeaf doesn't offer (e.g. hardscaping / a patio) can still score `hot`. See the
  "Edge cases & learnings" note in [`workflows/score_and_prioritize.md`](../workflows/score_and_prioritize.md).
