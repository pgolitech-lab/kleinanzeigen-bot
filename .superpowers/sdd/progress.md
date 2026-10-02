# CRM Client Profile — SDD Progress

Branch start commit: 216df4a

## Tasks
- [x] Task 1: DB — client_profiles table + get/upsert
- [x] Task 2: DB — list_threads_for_client + deal_brief_json
- [x] Task 3: API — extend GET /clients/{email}/history
- [x] Task 4: API — POST /clients/{email}/profile
- [x] Task 5: Frontend — client.js + router
Task 1: complete (commits 216df4a..2aae1f5, review clean)
Task 2: complete (commits 2aae1f5..8e45748, review: minor scope overlap with Task 3 — api_ma.py already has partial deal_brief, Task 3 will replace)
Task 3: complete (commits 8e45748..94d75e8, review clean)
Task 4: complete (commits 94d75e8..8f9ff50, review clean)
Task 5: complete (commits 8f9ff50..b761d72, review clean)
Fix: sold_count, max_length, dict(r).get, single get_conn (commit 34cdf14, 213 tests pass)

## Plan: 2026-07-15 MA Ingestion & Manual-Flow Correctness (Track B, Increment 1)
Branch: track-b-ma-negotiation  Base: 96d20a9
- [x] Task 1: reminders exclude active autopilot (Bug 10)
- [x] Task 2: sold-reopen guard (Bug 6)
- [x] Task 3: related-inquiries by email (Bug 8)
- [x] Task 4: content-level dedup (Bug 9)
- [x] Task 5: classifier system-message tightening (Bug 7)
Task 1: complete (commits 96d20a9..a8d706b, review clean)
Task 2: complete (commits a8d706b..5ce30be, review clean)
Task 3: complete (commits 5ce30be..75405f0, review clean)
Task 4: complete (commits 75405f0..0453a85, review clean)
Task 5: complete (commits 0453a85..7fa180b, review clean; MINOR: SYSTEM_BODY_PATTERNS 1-2 overlap, harmless)

## Final whole-branch review: READY TO MERGE
Plan 2 TODO (pre-existing, found in final review): apply_detection_to_messages (db_sales.py:231) should call stop_thread_autopilot like mark_thread_sold does — autopilot resurrection on scout-detected sale. Not a Plan-1 regression.
Minor accepted: SYSTEM_BODY_PATTERNS 1-2 overlap harmless.

MERGED to main (f706087, --no-ff) + DEPLOYED: 256 tests pass on main, service restarted active, clean start verified. 2026-07-15.

## Plan: 2026-07-15 MA Negotiation Foundation (Track B, Increment 2)
Branch: track-b-increment-2  Base: f706087 (main)
- [x] Task 1: negotiation_state table + helpers
- [x] Task 2: autopilot config keys (cap + shadow)
- [x] Task 3: modules/guardrails.py pure functions
Inc2 Task 1: complete (commits 86e001f..814ee7b, review clean; minor: per-field UPDATE N+1, harmless)
Inc2 Task 2: complete (commits 814ee7b..6daa39b, review clean)
Inc2 Task 3: complete (commits 6daa39b..e1db760 + fix bd3059e, review: 1 Important fixed [extract_json_object last-object] + re-review clean)

MERGED to main (02f1755, --no-ff) + DEPLOYED: 279 tests pass, service active, negotiation_state table created. 2026-07-16.
Minor deferred to Inc3: upsert_negotiation_state N+1 UPDATE; utcnow deprecation.
