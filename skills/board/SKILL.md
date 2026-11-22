---
name: board
description: Compose the Hermes app's Playground board (sessions to pick up, prompts to try, numbers, notes, links) and publish it with app_board_publish.
---

# Playground board

The app's Playground tab shows one board that you compose. Publish it with the tool
`app_board_publish` (toolset `hermes_app`): `{"board": {...}}`. Each call replaces the whole board.
The tool returns `{"ok": true, "blocks": n}` or `{"ok": false, "error": "..."}` naming the first
invalid field; fix that field and call it again.

## Shape

```json
{"title": "Sunday", "summary": "Two threads want you today.",
 "sections": [{"title": "Pick up", "blocks": [ ... ]}]}
```

Keep it short: at most 4 sections, a handful of blocks each. Hard limits: 8 sections, 40 blocks,
400 characters per string (1200 for a `note` text). Do not set `generated_at`, `job_id` or
`session_id`; the tool stamps them.

## Blocks

Every block has `type`; optional `tone` (`live`, `info`, `idle`, `error`) and `action`.

- `session`: `session_id`, `title`, `reason`. A session worth picking up. Use a real session id from
  your session search, and give a concrete reason it matters now ("you stopped before choosing X").
  Never add a session without a reason.
- `prompt`: `title`, `detail`?, `prompt`. A draft the user can send as a new session; the app puts
  `prompt` in the input without sending. Write it so it works on its own.
- `stat`: `label`, `value`. One number or short fact. Consecutive stats form a grid.
- `note`: `text` (markdown). A short paragraph. Use sparingly.
- `link`: `title`, `url`, `detail`?. Opens in the browser.
- `item`: `title`, `detail`?, `time`? (ISO 8601; shows on a timeline).

`action` opens something on tap: `{"open": "session", "session_id": "..."}`,
`{"open": "prompt", "prompt": "..."}`, `{"open": "url", "url": "https://..."}`,
`{"open": "tab", "tab": "sessions" | "jobs" | "host"}`. `session` and `prompt` blocks already carry
their own target, so `action` is ignored on them.

## Scheduling

Run this as a cron job so the board stays fresh: create a job (for example every morning) whose
prompt says to review recent sessions and jobs, then compose and publish the board with this skill.
The app's REBUILD button runs that job again; it is offered when the board was published from a cron
session.

Plugin toolsets are on by default. If a job restricts `enabled_toolsets` (or `platform_toolsets.cron`
is an explicit list), include `hermes_app` so the tool is available.
