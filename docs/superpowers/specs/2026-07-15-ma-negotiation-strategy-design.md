# MA Negotiation Strategy & Algorithm Correctness — Design

- **Date:** 2026-07-15
- **Status:** Approved (brainstorming), pending spec review → implementation plan
- **Track:** B (of a larger effort: A = CRM UI redesign, B = algorithm/strategy, C = legacy web auth/security)
- **Owner:** operator (single-owner CRM)
- **Approach chosen:** #2 — *structured negotiation state + deterministic guardrails* (over #1 guardrails-only and #3 full agent redesign)

---

## 1. Context & current reality

Kleinanzeigen email-bot CRM: ingests buyer inquiries via Gmail IMAP across **5 active accounts**, uses Claude (Sonnet `claude-sonnet-4-6` for drafts/autopilot, Haiku for classify/translate) to draft/auto-send replies, tracked through a pipeline in a Telegram Mini App.

Verified operational state (2026-07-15):
- `send_mode = production` — **the system sends real emails to real buyers right now.**
- `reminders_enabled = 1`, `polling_paused = 0`, `max_discount_percent = 10`, `gmail_poll_interval_sec = 60`.
- Volume: ~26 incoming / ~9 sent per 7 days; 2051 messages total; 49 sold.
- **Autopilot has never sent a single message** (`autopilot_replies_total = 0`, active = 0). It is fully built but never trusted/enabled in production. Everything today is manual (operator-approved) drafts.

**Interpretation of the request** ("внедри более корректный алгоритм и стратегию работы с МА, сделай готовый продукт"): make the negotiation engine *correct and trustworthy* across the **full strategy** — manual flow, autopilot, and the transitions between them — so autopilot can finally be enabled safely, and the daily manual flow is more correct.

### Core principle
**Code owns the numbers; the LLM owns the prose.** Prices, floor, offer ratchet, escalation triggers, concurrency, and language become deterministic code invariants. The LLM proposes message text and a *proposed next offer*; code validates, clamps, or escalates.

### Autonomy model (decided)
**Balanced.** Autopilot negotiates autonomously down to the floor; escalates to the operator on commit / contact request / threat / below-floor pressure / anomaly / message cap. The bot never silently drops a thread.

### Concession policy (decided)
**Flexible within guardrails.** The LLM proposes the concession step per situation; code guarantees monotonic non-increasing *our* offer and `>= floor`. No rigid fixed % steps.

---

## 2. Goals & non-goals

**Goals**
1. Deterministic safety invariants around negotiation (floor, language, anti-double-send, robust parsing).
2. Explicit persisted negotiation state shared by manual + autopilot; no more reconstructing deal state from raw text.
3. Balanced-autonomy escalation taxonomy, code-enforced.
4. Correctness fixes to the manual/ingestion flow (identity, dedup, classifier bypass, sold-reopen, reminders).
5. A safe, staged rollout that never risks the live manual flow or fires bad autopilot messages.

**Non-goals (this track)**
- The CRM UI redesign (Track A).
- Legacy `web/app.py` auth/security (Track C).
- Full tool-using agent rebuild (approach #3, rejected).
- Changing `send_mode` or business pricing policy (max_discount stays operator-controlled).

---

## 3. Design

### 3.1 Negotiation state model (new)

New persisted per-thread entity `negotiation_state` (new table keyed by `gmail_thread_id`, or extension of `thread_autopilot` — decided at plan time; leaning to a **new table** so manual threads also get state):

| Field | Type | Owner | Meaning |
|---|---|---|---|
| `gmail_thread_id` | TEXT PK | — | thread key |
| `phase` | TEXT | code | `opening` → `negotiating` → `closing` → `escalated` / `stopped` |
| `mode` | TEXT | code/operator | `manual` \| `auto` |
| `list_price_eur` | REAL | code | listed ad price (parsed once) |
| `floor_eur` | REAL | code | single reconciled floor (see 3.2) |
| `our_last_offer_eur` | REAL | **code only** | last price *we* actually stated; the ratchet anchor. Written only when an outgoing offer is actually sent. |
| `buyer_last_offer_eur` | REAL | code | last price buyer stated |
| `messages_sent` | INT | code | **persistent** autopilot counter (does not reset on restart/reactivation) |
| `escalation_reason` | TEXT | code | nullable; set when phase=`escalated` |
| `updated_at` | TEXT | code | — |

The LLM response schema gains a structured `proposed_offer_eur` (nullable) alongside the message text. Code:
- rejects/clamps `proposed_offer_eur < floor_eur`,
- forbids raising our own offer (`proposed_offer_eur > our_last_offer_eur` when we're conceding) — monotonic,
- on violation → clamp to floor OR escalate (never silently send a below-floor number).

### 3.2 Deterministic guardrails (each maps to a known bug)

| # | Guardrail (enforced in code, independent of LLM output) | Fixes |
|---|---|---|
| G1 | **Floor check before send:** verify the outgoing message's price (structured `proposed_offer_eur`, cross-checked against any € figure parsed from the text) is `>= floor_eur`; else block + escalate. | Bug 1 (floor only in prompt) |
| G2 | **Language validation:** detect language of outgoing client-facing text; assert `== client_lang`. Mismatch → do not auto-send; regenerate once, then escalate. Critical because autopilot has no human check. | Bug 11 (wrong-language ship) |
| G3 | **Concurrency:** autopilot path respects the same thread-busy / operator-lock as manual. If operator holds the lock or thread is busy → **defer** the autopilot reply (status `deferred`), do not send concurrently. | Bug 4 (double reply) |
| G4 | **Robust autopilot JSON extraction:** tolerate web_search narration (scan for the JSON block, not just "last text block"); on parse failure → create an operator review card (escalate) instead of aborting and leaving the message orphaned in `status='new'`. | Bug 12 (incoming aborts) |
| G5 | **Single reconciled floor:** `floor_eur = max(ad_brief.min_acceptable_eur, operator_floor)` (operator floor may only tighten, never loosen below ad-brief min). One floor passed to the model *and* enforced in code. | Bug 3 (two conflicting floors) |
| G6 | **Reliable ratchet:** `our_last_offer_eur` is written by code from the actually-sent offer, never read back from an ambiguous `deal_brief_json.negotiated_price_eur`. | Bug 2 (ratchet corruption) |

### 3.3 Escalation taxonomy (balanced autonomy, code-enforced)

Autopilot **stops, flips thread to `manual`, keeps/creates a review card, and notifies** on any of:
- **commit** — buyer ready to buy / "I'll take it".
- **contact** — buyer requests phone / WhatsApp / in-person meeting.
- **threat / scam** — abusive or fraud signals (optionally send one holding message, then escalate).
- **below-floor pressure** — buyer pushes below floor after we've stated a final price.
- **anomaly** — language mismatch (G2), price parse failure, off-topic, or model low-confidence / cannot answer.
- **cap** — persistent `messages_sent` reached the configured limit (config value, not a hardcoded `20` duplicated in 3 places).

Invariant: **the bot never silently abandons a thread** — every stop path produces an operator-visible artifact.

### 3.4 Manual ↔ autopilot transitions

- New inquiries default to `mode = manual` (operator reviews first contact).
- Operator hands a thread to `auto`; `negotiation_state` is seeded from the current negotiation (offers/floor/phase), so no continuity loss.
- Autopilot escalation flips the thread back to `manual` **with full state preserved** — operator takes over seamlessly (draft + deal context intact). Fixes Bug 13 (history/continuity loss).
- Operator may re-hand back to `auto` after intervening.

### 3.5 Manual-flow / ingestion correctness fixes

| Fix | Detail | Bug |
|---|---|---|
| **Client identity by email** | Primary key for "same client" / related-inquiries is `buyer_name` (email), not `buyer_display_name`. display_name is a secondary hint only. | Bug 8 (namesake merge) |
| **Content-level dedup** | In addition to Message-ID, dedup on a normalized (sender+thread+body-hash) key within a time window to catch KZ relay re-sends/forwards. | Bug 9 |
| **Classifier bypass tightening** | The "thread already has an `in` row → skip Haiku inquiry gate" shortcut still runs a cheap junk/system-message check; not a blanket pass. | Bug 7 |
| **Sold-reopen guard** | Thread-level `sold` flag: a new email into a sold thread does **not** resurrect autopilot and does **not** silently re-enter the pipeline — it routes to operator review. (Ad-level `mark_ad_sold` stays untouched: item may be resold to the next buyer.) | Bug 6 |
| **Reminders exclude autopilot** | `find_reminder_candidates` excludes threads with `thread_autopilot.active = 1`. | Bug 10 |

### 3.6 Rollout & safety (production system)

1. **TDD** — a test per guardrail (G1–G6), per escalation trigger, per state transition, and per ingestion fix. Repo already has strong `pytest` coverage on `api_ma` (`tests/test_api_ma_*`).
2. **DB backup** — back up `kleinanzeigen.db` before any schema migration (per `AGENTS.md`). Migration via existing `_add_column_if_missing` idempotent pattern; new table created idempotently in `init_db()`.
3. **Autopilot validation ladder (no-risk → risk):**
   - **Shadow mode** — on real incoming, autopilot generates + logs what it *would* send (including guardrail decisions) but sends nothing; operator reviews for a period.
   - **Redirect** — `send_mode=redirect` to `debug_email` for autopilot-driven threads.
   - **Production enable** — only after shadow + redirect look correct.
4. **Incremental delivery order:**
   1. Guardrails G1–G6 + manual-flow fixes (3.5) — improve the *currently running* manual flow, low risk, no behavior change for the operator except more safety.
   2. `negotiation_state` table + wiring into manual flow (state populated, not yet driving autopilot).
   3. Autopilot rework onto structured state + escalation taxonomy, behind the validation ladder.
5. **Rollback** — each increment is a separate commit on a feature branch; schema additions are additive (no destructive migration); `send_mode` untouched; autopilot stays off until explicitly enabled.

---

## 4. Bug → fix traceability (the 13 findings)

| Bug | Summary | Addressed by |
|---|---|---|
| 1 | No code-level floor enforcement | G1, 3.1 |
| 2 | Unreliable `last_our_price` ratchet | G6, 3.1 |
| 3 | Two conflicting floors | G5 |
| 4 | Autopilot bypasses busy/lock → double send | G3 |
| 5 | `messages_sent` resets on reactivation; cap hardcoded ×3 | 3.1 (persistent counter), 3.3 (config cap) |
| 6 | Sold threads silently reopen | 3.5 sold-reopen guard |
| 7 | Classifier bypass unconditional | 3.5 classifier tightening |
| 8 | Client match by display_name only | 3.5 identity-by-email |
| 9 | Dedup Message-ID only | 3.5 content-level dedup |
| 10 | Reminders don't exclude active autopilot | 3.5 reminders exclusion |
| 11 | Wrong-language text can ship | G2 |
| 12 | Fragile autopilot JSON extraction aborts incoming | G4 |
| 13 | History drops structured deal state → weak continuity | 3.1 + 3.4 (shared persisted state) |

---

## 5. Open questions for plan phase
- `negotiation_state` as a **new table** vs extending `thread_autopilot` (leaning new table so manual threads also carry state).
- Exact config keys/defaults for autopilot message cap and shadow-mode flag.
- Whether shadow-mode logs surface in the Mini App (Track A) or just server logs initially (initially: server logs + optional Telegram digest).

## 6. Testing strategy summary
- Unit: guardrails (floor clamp, language mismatch, ratchet monotonicity, JSON-repair fallback), state transitions, escalation classification.
- Integration: full incoming→draft→send path in `disabled`/`redirect` modes; autopilot shadow path; sold-reopen; reminder exclusion; dedup; identity.
- Regression: keep all existing `tests/` green; add `tests/test_negotiation_state.py`, `tests/test_autopilot_guardrails.py`, `tests/test_ingestion_correctness.py`.
- Gate: `python3 -m pytest -q` green before each commit (baseline was 234–236 passing).
