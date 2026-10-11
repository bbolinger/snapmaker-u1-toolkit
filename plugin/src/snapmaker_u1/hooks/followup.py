"""Send a short message AFTER Hermes has delivered a reply and its attachments.

Hermes delivers a reply's text first and its photos and documents after it.
A question at the end of the text therefore lands above the pictures, out of
view (operator 2026-10-11, the bed-clear question). The reply hook queues the
question here instead; a wrapper around the live adapter class's
``on_processing_complete`` (the lifecycle step Hermes runs once the whole
reply, attachments included, has been delivered) sends it as the last message
in the chat, in the reply's thread, flagged to notify.

``install(gateway)`` patches each live adapter class once, from the
pre_gateway_dispatch hook. ``ready(platform)`` tells the reply hook whether a
patched adapter will flush the queue; when it will not, the caller keeps the
question in the reply text instead, so the operator is never left without it.
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# A queued message older than this is stale (its turn failed before delivery
# finished); it is dropped rather than sent into a later conversation.
TTL_SECONDS = 600.0

_lock = threading.Lock()
_pending: dict[tuple[str, str], list[tuple[float, str]]] = {}
_ready_platforms: set[str] = set()


def platform_name(platform: Any) -> str:
    """'telegram' from a Platform enum, its value, or a plain string."""
    return str(getattr(platform, "value", platform) or "").strip().lower()


def queue(platform: Any, chat_id: Any, text: str) -> None:
    key = (platform_name(platform), str(chat_id))
    with _lock:
        _pending.setdefault(key, []).append((time.time(), text))


def pop(platform: Any, chat_id: Any) -> list[str]:
    """Take this chat's queued messages, oldest first, dropping stale ones."""
    key = (platform_name(platform), str(chat_id))
    now = time.time()
    with _lock:
        entries = _pending.pop(key, [])
    return [text for queued_at, text in entries if now - queued_at <= TTL_SECONDS]


def ready(platform: Any) -> bool:
    return platform_name(platform) in _ready_platforms


def _thread_metadata(event: Any) -> dict | None:
    """The reply's thread metadata, marked notify (as Hermes marks a final
    reply), so the question lands in the same thread and pings the phone."""
    try:
        from gateway.platforms.base import (_mark_notify_metadata,
                                            _thread_metadata_for_event)
        return _mark_notify_metadata(_thread_metadata_for_event(event))
    except Exception:
        return {"notify": True}


async def _flush(adapter: Any, event: Any) -> None:
    try:
        source = getattr(event, "source", None)
        chat_id = getattr(source, "chat_id", None)
        if chat_id is None:
            return
        texts = pop(getattr(source, "platform", ""), chat_id)
        if not texts:
            return
        metadata = _thread_metadata(event)
        for text in texts:
            await adapter.send(str(chat_id), text, metadata=metadata)
        logger.info("snapmaker_u1 followup: sent %d follow-up message(s) after "
                    "the reply", len(texts))
    except Exception:
        logger.warning("snapmaker_u1 followup: sending the follow-up failed",
                       exc_info=True)


def _patch_class(cls: type) -> bool:
    """Wrap ``cls.on_processing_complete`` once. False when the adapter has no
    async on_processing_complete to wrap."""
    if getattr(cls, "_u1_followup_patched", False):
        return True
    original = getattr(cls, "on_processing_complete", None)
    if original is None or not inspect.iscoroutinefunction(original):
        return False

    async def on_processing_complete(self, event, *args, **kwargs):
        try:
            return await original(self, event, *args, **kwargs)
        finally:
            await _flush(self, event)

    on_processing_complete.__wrapped__ = original  # type: ignore[attr-defined]
    on_processing_complete.__doc__ = original.__doc__
    cls.on_processing_complete = on_processing_complete
    cls._u1_followup_patched = True
    return True


def install(gateway: Any) -> None:
    """Patch every live adapter's class; record which platforms will flush."""
    adapters = getattr(gateway, "adapters", None) or {}
    for key, adapter in dict(adapters).items():
        try:
            if _patch_class(type(adapter)):
                _ready_platforms.add(platform_name(key))
        except Exception:
            logger.warning("snapmaker_u1 followup: could not patch %r", key,
                           exc_info=True)
