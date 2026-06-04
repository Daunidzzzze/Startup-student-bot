"""
OpenAI integration — wraps chat completion calls with role prompts and history.
"""
from __future__ import annotations

import time
from typing import Optional

from openai import AsyncOpenAI

from config import MAX_CONTEXT_MESSAGES, OPENAI_MODEL


class AIService:
    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)

    async def get_response(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        model: str = OPENAI_MODEL,
    ) -> tuple[str, int]:
        """
        Returns (reply_text, latency_ms).
        `history` is a list of dicts with keys 'role' and 'content'
        where role is 'student' or 'agent' — we normalise to 'user'/'assistant'.
        """
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # Add conversation history (last MAX_CONTEXT_MESSAGES entries)
        for msg in history[-MAX_CONTEXT_MESSAGES:]:
            oai_role = "user" if msg["role"] == "student" else "assistant"
            messages.append({"role": oai_role, "content": msg["content"]})

        # Add current user message
        messages.append({"role": "user", "content": user_message})

        t0 = time.monotonic()
        response = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=1024,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        reply = response.choices[0].message.content.strip()
        return reply, latency_ms
