"""OAuth providers: data the app renders as tiles and a generic form (plan 0009).

Each entry has public parts (``id``, ``name``, ``kind``, ``help``, ``flows``, ``fields``) that
``public_list`` serves, and a server-only ``config`` that ``sites.put`` copies into ``site.json``.
``fields[].key`` may only be ``client_id``, ``client_secret``, ``scopes``, ``redirect_uri`` or
``redirect_param``; ``put`` drops every other key of a provider request. Adding a provider is an
entry here, with no app release.
"""
import copy

FIELD_KEYS = ("client_id", "client_secret", "scopes", "redirect_uri", "redirect_param")
CONFIG_KEYS = ("auth_url", "token_url", "extra_params", "token_auth", "pkce", "refresh_scope")

_EBAY_SCOPES = " ".join(
    "https://api.ebay.com/oauth/api_scope" + s for s in (
        "", "/sell.inventory", "/sell.analytics.readonly", "/sell.fulfillment.readonly",
        "/sell.account.readonly"))

PROVIDERS = {
    "google": {
        "id": "google", "name": "Google", "kind": "oauth", "flows": ["host", "safari"],
        "help": "OAuth client (Desktop app) from console.cloud.google.com → APIs & Services → Credentials.",
        "fields": [
            {"key": "client_id", "label": "Client ID", "required": True},
            {"key": "client_secret", "label": "Client secret", "secret": True, "required": True},
            {"key": "scopes", "label": "Scopes"},
            {"key": "redirect_uri", "label": "Redirect URI", "default": "http://localhost"},
        ],
        "config": {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "extra_params": "access_type=offline&prompt=consent",
            "redirect_uri": "http://localhost",
        },
    },
    "ebay": {
        "id": "ebay", "name": "eBay", "kind": "oauth", "flows": ["safari"],
        "help": "Keys and RuName from developer.ebay.com → Application Keys (Production).",
        "fields": [
            {"key": "client_id", "label": "App ID", "required": True},
            {"key": "client_secret", "label": "Cert ID", "secret": True, "required": True},
            {"key": "redirect_param", "label": "RuName", "required": True},
            {"key": "redirect_uri", "label": "Accept URL", "default": "https://localhost/ebay", "required": True},
            {"key": "scopes", "label": "Scopes", "default": _EBAY_SCOPES},
        ],
        "config": {
            "auth_url": "https://auth.ebay.com/oauth2/authorize",
            "token_url": "https://api.ebay.com/identity/v1/oauth2/token",
            "token_auth": "basic",
            "pkce": True,  # unconfirmed: V1 decides
            "refresh_scope": True,  # unconfirmed: V1 decides
        },
    },
}


def public_list() -> list[dict]:
    """The public part of every provider (never ``config``)."""
    return [{k: copy.deepcopy(p[k]) for k in ("id", "name", "kind", "help", "flows", "fields")}
            for p in PROVIDERS.values()]


def get(provider_id) -> dict | None:
    return PROVIDERS.get(provider_id) if isinstance(provider_id, str) else None
