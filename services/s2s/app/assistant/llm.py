"""The assistant's LLM: an OpenAI-compatible chat server (Qwen on vLLM here), thinking off."""
from __future__ import annotations

import os
import re

import aiohttp

# The assistant uses the call translator's chat LLM unless told otherwise.
LLM_URL = (os.environ.get("ASSISTANT_LLM_URL") or (os.environ.get("TRANSLATE_URL", "") if os.environ.get("TRANSLATE_API") == "chat" else "")).rstrip("/")
LLM_MODEL = os.environ.get("ASSISTANT_LLM_MODEL") or os.environ.get("TRANSLATE_MODEL", "")
LLM_KEY = os.environ.get("ASSISTANT_LLM_KEY", "")


def configured() -> bool:
    return bool(LLM_URL and LLM_MODEL)


async def chat(http: aiohttp.ClientSession, messages: list[dict], *, max_tokens: int = 300, temperature: float = 0.3,
               tools: list[dict] | None = None, json_schema: dict | None = None) -> dict:
    """One chat completion; returns the assistant message ({content, tool_calls?}). With `json_schema`,
    the reply is JSON matching it (vLLM's guided decoding)."""
    body = {"model": LLM_MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False}}
    if tools:
        body["tools"] = tools
    if json_schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": json_schema.get("title", "reply"), "schema": json_schema}}
    headers = {"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {}
    async with http.post(f"{LLM_URL}/chat/completions", json=body, headers=headers) as r:
        if r.status != 200:
            raise RuntimeError(f"LLM HTTP {r.status}: {(await r.text())[:200]}")
        data = await r.json()
    return data["choices"][0]["message"]


_MD = [(re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),          # [text](link) -> text
       (re.compile(r"\S+@\S+\.\S+"), ""),                      # email addresses aren't spoken
       (re.compile(r"\[\d+\]"), ""),                           # citation markers
       (re.compile(r"[*_`#>]+"), ""),                          # emphasis, headings, quotes
       (re.compile(r"^\s*[-•]\s+", re.M), ""),                 # bullets
       (re.compile(r"\(\s*\)"), ""),
       (re.compile(r"\s*\n+\s*"), " "),
       (re.compile(r"\s{2,}"), " ")]


def to_speech(text: str) -> str:
    """Model text -> something a voice can read: no markdown, links, emails or citation marks."""
    for pat, rep in _MD:
        text = pat.sub(rep, text)
    return text.strip()
