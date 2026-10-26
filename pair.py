"""``hermes app pair``: print a QR code and link that configure the Hermes app.

The link is ``hermes://pair?u=<base url>&k=<api key>&n=<name>``. The key is the API server key,
read through Hermes's secret routes (never from a file). The base URL is ``--url`` or
``http://<API_SERVER_HOST or hostname>:<API_SERVER_PORT or 8642>``.
"""
import os
import socket
import sys
from typing import Optional
from urllib.parse import quote

from . import sites

DEFAULT_PORT = "8642"


def default_url() -> str:
    host = (os.getenv("API_SERVER_HOST") or "").strip()
    if host in ("", "0.0.0.0", "::"):
        host = socket.gethostname()
    port = (os.getenv("API_SERVER_PORT") or "").strip() or DEFAULT_PORT
    return f"http://{host}:{port}"


def build_link(url: str, key: str, name: str) -> str:
    return f"hermes://pair?u={quote(url, safe='')}&k={quote(key, safe='')}&n={quote(name, safe='')}"


def _api_key() -> str:
    from agent.secret_scope import get_secret

    return get_secret("API_SERVER_KEY", "") or ""


def run(url: Optional[str] = None, name: Optional[str] = None, out=None) -> int:
    out = out or sys.stdout
    key = _api_key()
    if not key:
        print("API_SERVER_KEY is not set; the API server cannot run without it.", file=sys.stderr)
        return 1
    link = build_link((url or default_url()).strip().rstrip("/"), key, (name or socket.gethostname()).strip())
    import segno

    segno.make(link, error="m").terminal(out=out, compact=True)
    print(link, file=out)
    return 0


def _pair(args) -> int:
    return run(getattr(args, "url", None), getattr(args, "name", None))


def _setup(parser) -> None:
    sub = parser.add_subparsers(dest="app_command")
    pair = sub.add_parser("pair", help="Print a QR code that pairs the Hermes app with this host")
    pair.add_argument("--url", help="Base URL the app should use (default: http://<host>:8642)")
    pair.add_argument("--name", help="Name shown in the app (default: this host's name)")
    pair.set_defaults(func=_pair)
    sites.setup_cli(sub)
    parser.set_defaults(func=lambda args: parser.print_help() or 0)


def register(ctx) -> None:
    ctx.register_cli_command("app", "Pair the Hermes iOS app", _setup, description="hermes app pair")
