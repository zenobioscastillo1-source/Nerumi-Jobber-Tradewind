# Workflow: Score & Prioritize (read-only)

## Objective
Rank each newly-surfaced Jobber **Request** by how worth chasing it is — so a capacity-constrained owner
spends time on the right leads first — and for the low-priority ones, queue a **polite deferral reply**
(a warm "we're near capacity / waitlist" note) instead of letting them go silent. The score is
**internal triage** and stays in the Sheet; the only thing a customer ever sees is the deferral draft,
which is held for approval. **Nothing is written to Jobber and nothing is sent** — execution is Layer 3.

> **Where this sits:** runs after [ingest_and_surface.md](ingest_and_surface.md) (Layer 1) has surfaced
> the Requests, and reuses [draft_and_propose.md](draft_and_propose.md) (Layer 2) for the deferral reply.
> Still read-only to Jobber, still sending nothing.

## Guardrails (read before running)
- **Read-only to Jobber; no new scopes; no send capability; no extra Jobber calls.** Scoring runs over
  the batch Layer 1 already fetched (`.tmp/new_requests.json`) — it adds no API cost.
- **Scoring is internal triage, NOT a customer-facing claim.** Like the duplicate/incomplete flags, the
  score never leaves the Sheet and therefore does **not** pass the knowledge grounding gate. The
  **deferral reply**, however, *is* customer-facing → it goes through the Layer 2 grounded-drafting path
  (`check_draft_grounding.py`) like any other message.
- **Tools emit signals; judgment is the agent's.** `score_lead.py` produces a deterministic score +
  *suggested* tier; the agent confirms or overrides `priority_tier` and writes the one-line
  `priority_reason`, reasoning over `knowledge/qualification_criteria.md` for the edge calls.
- **Criteria are owner-confirmed (sample for the demo).** The weights live in `tools/scoring_rules.py`;
  the human "why" lives in `knowledge/qualification_criteria.md`. Never invent the owner's priorities —
  if the criteria file is gapped/unconfirmed, scores still compute from the code defaults but you must
  say the criteria aren't owner-confirmed yet.
- **Escalate on doubt.** Anything ambiguous (a tier you're unsure of, an out-of-area lead, an
  unrecognised source) → note it for the owner. "I wasn't sure, here's why" is always correct.

## Required inputs / config (.env)
- Everything Layer 1 needs, plus `SCORING_TAB` (default `Prioritized Leads`) — created automatically on
  first write. The deferral drafts use `DRAFTS_TAB` (`Follow-up Drafts`) from Layer 2.
- `knowledge/qualification_criteria.md` (+ the rest of `knowledge/` for the deferral draft).

## Preconditions
- Layer 1 has run: `Requests` populated, cursor advanced, and `.tmp/new_requests.json` holds the batch.

## Steps

1. **Load + validate the criteria.**
   ```
   python tools/knowledge_loader.py            # -> .tmp/knowledge_context.json
   ```
   Confirm `qualification_criteria.md` is usable (status confirmed/sample). If it's gapped, scores still
   compute from `tools/scoring_rules.py` defaults — note that the priorities aren't owner-confirmed yet.

2. **Score each Request (deterministic).** For every request in the batch:
   ```
   python tools/score_lead.py --request-id <rid> --requests-file .tmp/new_requests.json
   ```
   (optionally `--matches-file .tmp/dup_<cid>.json` to mark an existing customer.) → `{score,
   suggested_tier, signals, top_signals, reasons, recommended_action}`.

3. **Agent assessment (judgment).** For each Request:
   - `priority_tier`: start from `suggested_tier`; override only with a stated reason grounded in
     `qualification_criteria.md` (e.g. bump a recurring-maintenance lead the owner values).
   - `priority_reason`: one line, drawn from `reasons` (e.g. "Referral, in-area install, time-sensitive").
   - `recommended_action`: chase today / follow up / defer to waitlist.

4. **Surface rows (idempotent upsert), highest score first.** Process leads in **score-descending**
   order so the tab reads as a ranked list:
   ```
   python tools/log_to_sheet.py --target scoring --row-file <scoring_row.json>
   ```
   Dedup is on `request_id`; the first write applies the owner status dropdown
   (chasing / deferred / reviewed). Set `status=scored`, `surfaced_at`=now.

5. **Queue a deferral reply for cool/defer leads (reuses Layer 2).** For each `cool`/`defer` lead, run
   the Layer 2 drafting path aimed at a waitlist next step, then record `deferral_drafted=yes` on the
   scoring row:
   ```
   python tools/build_draft_context.py --request-id <rid>     # -> brief
   # AGENT writes a warm "near capacity / may we add you to our waitlist?" email+SMS in voice.md,
   #   grounded ONLY in confirmed knowledge; anything it can't ground -> ungrounded_flags.
   python tools/check_draft_grounding.py --draft-file <draft_row.json> --emit-row .tmp/_draft_row.json
   python tools/log_to_sheet.py --target drafts --row-file .tmp/_draft_row.json
   ```
   The gate enforces the no-invented-facts rule exactly as in Layer 2; a failed draft is never logged.

6. **Summarize for the owner.** Report counts per tier (hot/warm/cool/defer), the top leads to chase now
   with their `priority_reason`, how many deferral replies were drafted, and anything you escalated
   (unrecognised source, out-of-area, a tier you were unsure of). **Take no external action.**

## Sheet schema (canonical order in tools/sheet_schema.py)
- **Prioritized Leads tab** — dedup key `request_id`: `request_id · created_at · request_title ·
  request_source · client_id · client_name · score · priority_tier · priority_reason · top_signals ·
  recommended_action · deferral_drafted · status · surfaced_at · owner_notes`
- Deferral replies land on the existing **Follow-up Drafts** tab (Layer 2 schema), with the
  approve/edit/reject dropdown.

## Scoring model (where to tune)
`tools/scoring_rules.py` is the single source of truth for the weights: source → points, job-value
keyword tiers, urgency keywords, service-area cities (sync with `service-area.md`), contactability, and
the tier thresholds (hot ≥75 / warm ≥55 / cool ≥35 / else defer). Tune there; document the owner's real
priorities in `knowledge/qualification_criteria.md`.

## Edge cases & learnings
*(Append findings over time — propose changes to the owner, don't silently overwrite, per CLAUDE.md.)*
- **Unrecognised source** → neutral points + flag it; ask the owner how they rank that channel, then add
  it to `SOURCE_WEIGHTS`.
- **Empty/short title** → scores on source + area + contactability only (value defaults to neutral); say
  the title gave no job signal.
- **Out-of-area lead** → scored down and surfaced with a travel-surcharge note; never promise service.
- **No free-text message body** on the Request limits the value/urgency read. Confirm in GraphiQL whether
  a notes/message field exists; if so, add it to `REQUESTS_QUERY` and feed it to the keyword signals.
- **Criteria file gapped** → scores still compute from code defaults, but say plainly the priorities are
  not owner-confirmed yet (same honesty habit as the knowledge grounding gate).
- **Google Sheets read quota (60 reads/min/user).** Each `log_to_sheet.py` call does a few bootstrap
  reads, so logging a large batch in a tight burst can trip a `429 RATE_LIMIT_EXCEEDED`. Normal layer
  runs are well under it; if you batch-log many rows fast, pace the calls or back off and retry on 429
  (the `demo/scoring.py` / `demo/full.py` harnesses do this). It is a rate limit, not a logic error.
- **A service we don't offer can still score `hot`.** The value signal rewards keywords like
  *install / patio / retaining wall* (`HIGH_VALUE_KEYWORDS`) but does **not** cross-check
  `knowledge/services.md`'s *Explicitly NOT offered* list — so a hardscaping/patio lead can land `hot`
  (seen in the demo). Scoring measures *value/fit*, not *can-we-do-it*. Until a services-offered check
  exists, the agent should down-rank or flag a lead for a non-offered service and keep any follow-up
  **claim-light** (engage / qualify, but never promise work we don't do). Candidate fix: a new signal
  that reads the offered/not-offered list from `services.md`.
