# hermes_app

Gateway plugin for the Hermes iOS app. It turns gateway and cron notices into Inbox entries,
serves them to the app together with attachment upload, message search and the agent's todo
list, and pairs the app with your host through a QR code. Requires Hermes 0.22.2 or newer and the
API server (`API_SERVER_KEY`).

## Routes

All routes live on the API server's own port (default 8642) and need the same
`Authorization: Bearer $API_SERVER_KEY` as the rest of the API server. Without it they return `401`.

- `GET /app/v1/capabilities`: `{"plugin": "hermes_app", "version": "2.0.0", "features": ["notices", "devices", "upload", "search", "todos"]}`. The app calls this first; a `404` means the plugin is not installed and the app hides the features above.
- `GET /app/v1/notices?limit=100`: `{"notices": [...]}`, newest first.
- `POST /app/v1/devices`: `{token, environment?, kind?}`. `environment` is `production` (default) or `sandbox`; `kind: "widget"` stores the token in `widget_devices.json`, apart from the alert devices. The plugin only stores tokens; it sends no push.
- `POST /app/v1/upload`: raw file body with `Content-Type` and `X-Filename` (percent-encoded), up to 10 MB (the API server's body limit; `413` above). Stores `~/.hermes/uploads/<YYYY-MM-DD>/<8 hex>-<name>` and returns `201 {"path", "name", "size", "mime"}`. `path` is absolute and goes into the run input for the vision or file tools.
- `GET /app/v1/search?q=&limit=30`: full-text match over user and assistant messages, limit at most 50. Returns `{"hits": [{"session_id", "session_title", "message_id", "role", "snippet", "timestamp"}]}`. The snippet marks matches with `**`. Queries under 2 characters give `{"hits": []}`.
- `GET /app/v1/todos?session_id=`: the agent's latest todo list for a session, kept in memory by the `post_tool_call` hook (lost on restart). `200 {"session_id", "todos"}`, `404 {"error": "no plan"}`, `400` without `session_id`.

## Events for push providers

After storing a notice the plugin emits the plugin event `hermes_app:notice` with the notice dict as
payload (`id`, `kind`, `title`, `body`, `job_id`, `created_at`). Another plugin subscribes with
`ctx.subscribe("hermes_app:notice", callback)` and can send a push. This plugin sends none. Device
tokens registered through `POST /app/v1/devices` are in `~/.hermes/hermes_app/devices.json`.

## Pairing

```sh
hermes app pair [--url URL] [--name NAME]
```

Prints a QR code and the link `hermes://pair?u=<base url>&k=<api key>&n=<name>`. The app scans the
code or opens the link. The base URL is `--url`, else `http://<API_SERVER_HOST or hostname>:<API_SERVER_PORT or 8642>`.
The key is the API server key, read through Hermes's secret routes. Treat the code like a password.

## Install

1. Install the plugin into `~/.hermes/plugins/hermes_app/` and enable it (`hermes plugins enable hermes_app`).
2. Set `HERMES_APP_HOME_CHANNEL` (any non-empty value, for example `app`) in your Hermes env to enable the
   platform and use it as the delivery target for cron jobs and gateway notices (`deliver: hermes_app`).
3. Restart the gateway: `hermes gateway restart`.

## What it does on your machine

- Serves the routes above on the API server's port. It opens no listener of its own.
- Reads `~/.hermes/state.db` read-only for search.
- Writes uploads to `~/.hermes/uploads/`.
- Stores notices and device tokens in `~/.hermes/hermes_app/` (`notices.jsonl`, `devices.json`, `widget_devices.json`; override the folder with `HERMES_APP_DATA_DIR`).
- Keeps the agent's todo lists in memory.
- Makes no network calls, runs no shell commands, starts no background processes, sends no telemetry.
- `hermes app pair` reads `API_SERVER_KEY` through `agent.secret_scope` and prints it inside the link.

## Files

- `plugin.yaml`: manifest.
- `__init__.py`: exports `register(ctx)`.
- `adapter.py`: platform adapter (`send()`), event emit, `register(ctx)`.
- `routes.py`: the `/app/v1` routes and their auth wrapper.
- `pair.py`: `hermes app pair`.
- `store.py`: notice classifier, JSONL notice store, device lists.
- `upload.py`, `search.py`, `todos.py`: upload storage, search query, todo list hook.
- `tests/`: pytest tests.

## Tests

```sh
~/.hermes/hermes-agent/venv/bin/python -m pytest plugin/tests
```
