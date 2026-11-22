"""Third-party agent providers (stage 1: Claude Code)."""
from . import claude_code

PROVIDERS = {claude_code.ID: claude_code}


def get(provider_id):
    return PROVIDERS.get(provider_id)


def describe_all() -> list:
    return [p.describe() for p in PROVIDERS.values()]
