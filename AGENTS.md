# AGENTS.md — kleinanzeigen-bot

You are working on the PRODUCTION server (192.168.88.28). The service is live. You have
full access: edit code, run tests, use sudo, restart services, commit and push — but follow
the safety rules below.

## What this project is

A system that manages buyer conversations for Kleinanzeigen.de listings across 5 seller
accounts. The Telegram bot sends notifications (one web_app button → Mini App screen) and
handles `/tasks`; all operator work happens in the Telegram Mini App (`web-app/`, GitHub
Pages) talking to a FastAPI app (`web/`) behind a cloudflared quick-tunnel (only `/api/ma/*`
and `/health` pass). The backend address reaches the MA as `?api=` in its URL. LLM calls go
through the Claude subscription (Claude Code CLI, `modules/llm.py`), not the Anthropic API.

## Layout

- `main.py`, `config.py`, `scheduler.py` — entry point, settings, background jobs (incl.
  scout_job every 6h).
- `database.py` — SQLite schema. Quirks: `messages` rows with direction='in' hold BOTH the
  buyer's question AND our reply; `gmail_message_id` = buyer's message, `sent_message_id` =
  ours; ~762 historical rows have status='archived' (similarity detection only, hidden from
  the pipeline).
- `modules/` — gmail ingest, parser, incoming/outgoing pipeline, drafts, Claude LLM replies
  (`claude.py`), autopilot, market scout (`*scout*`), DB helpers (`db_*.py`),
  `telegram_bot.py` (notifier only), `operator_lock.py`, `tg_init_data.py`.
- `web/` — FastAPI app; `api_ma.py` (+ `api_stock.py`) is the Mini App API surface.
- `web-app/` — Mini App frontend (design v3, 2026-10-20): vanilla ESM, no build step, no
  Bootstrap; tabs Inbox · Clients · Sales · Stock · More; layouts phone / flex (Flip cover)
  / split2 / split3 (Fold inner: list left, detail right); theme from Telegram themeParams.
  See CLAUDE.md «Mini App» for the send flow and the language invariant.
- `tests/` + `pytest.ini`; `docs/superpowers/plans/` — design history.

## Safety rules (non-negotiable)

- Send modes: disabled / redirect / production. NEVER switch the send mode or send real
  messages to buyers unless the owner explicitly asks.
- `bot.db` / `kleinanzeigen.db` are live production data. Back up before any schema change:
  `cp kleinanzeigen.db kleinanzeigen.db.bak-$(date +%F)`. Never DELETE/DROP without an
  explicit request.
- After changing bot code, restart with `sudo systemctl restart kleinanzeigen-bot` and check
  `journalctl -u kleinanzeigen-bot -n 30` for a clean start.
- Run `pytest` before committing. Commit style: conventional prefix + Russian summary, e.g.
  `feat(ma): ...` — see `git log` for examples. Push to origin (github.com/pgolitech-lab/
  kleinanzeigen-bot) after committing; GitHub Pages serves `web-app/` from this repo, so
  frontend changes go live on push.
- Mini App cache-bust: imports WITHOUT `?v=`; run `scripts/bump_cache.sh` after any
  `web-app/` change and commit `index.html`. Screens must check `mount.isConnected` after
  every `await`. Never send buyer text that failed the language check without the explicit
  operator checkbox (`force:true` only then).
- Every deep screen in the Mini App must have a visible back button (handled centrally by
  `backbar.js` via router show/hideBack — new screens get it automatically; don't remove it).
- The Mini App is the ONLY operator UI. One-active-screen UX rule: one task at a time.
