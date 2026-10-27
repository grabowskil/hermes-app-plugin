"""Hermes App platform: stores notices and announces them to other plugins.

Outbound only. The gateway's shutdown/online notices and cron deliveries arrive through
``send()`` (or the standalone sender when cron runs outside the gateway). Each one is stored
(``store.py``) and then published as the plugin event ``hermes_app:notice`` so a push provider
(for example a separate APNs plugin) can react. The HTTP routes for the app live in
``routes.py`` and run on the API server's own port; this adapter opens no listener.
"""
import logging
import os
from typing import Any, Dict, List, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from . import activity, budget, pair, routes, store, todos

logger = logging.getLogger(__name__)

NAME = "hermes_app"
EVENT = "notice"  # emitted as "hermes_app:notice"

_ctx = None  # set by register(); used to emit events


def _emit(notice: dict) -> None:
    """Publish the notice to event subscribers. Never raises."""
    if _ctx is None:
        return
    try:
        _ctx.emit(EVENT, notice)
    except Exception:
        logger.exception("hermes_app: could not emit %s (notice %s stored)", EVENT, notice.get("id"))


def publish(notice: dict) -> None:
    """append -> emit. Also used by the sites keep-alive loop."""
    store.append(notice)
    _emit(notice)


def _deliver(content: str) -> dict:
    """classify -> publish."""
    notice = store.classify(content)
    publish(notice)
    return notice


class HermesAppAdapter(BasePlatformAdapter):
    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(NAME))

    @property
    def name(self) -> str:
        return "Hermes App"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # Nothing to open: the routes are wired onto the API server (routes.py).
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        notice = _deliver(content)
        return SendResult(success=True, message_id=notice.get("id"))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": "Hermes App", "type": "dm"}


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    notice = _deliver(message)
    return {"success": True, "message_id": notice.get("id")}


def _home() -> str:
    return os.getenv("HERMES_APP_HOME_CHANNEL", "").strip()


def _env_enablement() -> Optional[dict]:
    home = _home()
    if not home:
        return None
    return {"home_channel": {"chat_id": home, "name": "Hermes App"}}


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_platform(
        name=NAME,
        label="Hermes App",
        adapter_factory=lambda cfg: HermesAppAdapter(cfg),
        check_fn=lambda: True,
        is_connected=lambda cfg: bool(_home()),
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="HERMES_APP_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        # Keep cron output in one notice; the app shows the full text.
        max_message_length=1_000_000,
        emoji="📱",
    )
    ctx.register_hook("post_tool_call", todos.on_post_tool_call)
    for hook, callback in (
        ("pre_llm_call", activity.on_pre_llm_call),
        ("pre_api_request", activity.on_pre_api_request),
        ("post_api_request", activity.on_post_api_request),
        ("post_tool_call", activity.on_post_tool_call),
        ("post_llm_call", activity.on_turn_end),
        ("on_session_end", activity.on_turn_end),
        ("pre_tool_call", budget.on_pre_tool_call),
        ("transform_tool_result", budget.on_transform_tool_result),
    ):
        ctx.register_hook(hook, callback)
    routes.register(ctx)
    pair.register(ctx)
