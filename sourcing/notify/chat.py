"""Google Chat notifications.

Prototype: an incoming webhook to a test space, @mentioning the requester.
Production: a Chat app (or the existing Chat door) sending a direct message.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("sourcing.notify")


def mention(email: str) -> str:
    # Chat accepts an email as the user id in mentions for users in the same
    # Workspace domain; anyone else just sees the plain email.
    return f"<users/{email}>" if email and "@" in email else ""


class ChatNotifier:
    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    async def send(self, text: str, *, to: Optional[str] = None) -> None:
        body = f"{mention(to)} {text}".strip() if to else text
        if not self._url:
            logger.info("[chat disabled] %s", body)
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._url, json={"text": body})
                resp.raise_for_status()
        except Exception as exc:  # notifications must never break a run
            logger.warning("Chat webhook failed: %r", exc)


CHAT_API = "https://chat.googleapis.com/v1"
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"


class ChatAppSender:
    """Posts messages as a Google Chat app (needed for cards with a text box: an
    incoming webhook can't receive a card's submit). Needs a Chat app set up by
    the client in their GCP project, the app added to `space` ("spaces/AAAA..."),
    and the runtime service account as the app's identity. Not set up yet
    (2026-10-22): the code handoff then sends only the form link."""

    def __init__(self, space: str) -> None:
        self._space = space

    def _token(self) -> str:
        import google.auth
        from google.auth.transport.requests import Request

        creds, _ = google.auth.default(scopes=[CHAT_BOT_SCOPE])
        creds.refresh(Request())
        return creds.token

    async def send_card(self, message: dict) -> None:
        import asyncio

        token = await asyncio.to_thread(self._token)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{CHAT_API}/{self._space}/messages", json=message,
                                     headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
