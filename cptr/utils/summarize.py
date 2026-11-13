"""Summarize older messages for context compaction.

Uses the same LLM as the active chat to generate a rolling summary.
Falls back to naive truncation if the LLM call fails.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The summary is the agent's only record of everything compacted away, so it must not be cut short.
SUMMARY_MAX_TOKENS = 4000
SUMMARY_RETRY_MAX_TOKENS = 8000
# What the summarizer reads: each message, and the whole transcript (characters).
MESSAGE_CHAR_LIMIT = 6000
TRANSCRIPT_CHAR_LIMIT = 120_000
CUT_OFF_NOTE = (
    "\n\n[This summary was cut off at its length limit; details from the end of the compacted history may be "
    "missing. Re-check files and state before relying on them.]"
)

DEFAULT_SUMMARIZE_PROMPT = (
    "Summarize this conversation history concisely. Include:\n"
    "- Key decisions made\n"
    "- Files created, modified, or deleted\n"
    "- Current task state and progress\n"
    "- Important context the assistant needs going forward\n"
    "- Any user preferences or instructions that should persist\n\n"
    "Be factual and specific. Use bullet points. Keep under 500 words."
)


def _get_summarize_prompt() -> str:
    """Read from config.toml [chat] section, fall back to default."""
    try:
        from cptr.utils.config import load_config

        config = load_config()
        return config.get("chat", {}).get("compact_summary_prompt", DEFAULT_SUMMARIZE_PROMPT)
    except Exception:
        return DEFAULT_SUMMARIZE_PROMPT


async def summarize_messages(
    messages: list[dict],
    existing_summary: str | None,
    connection: dict,
    model: str,
) -> str:
    """Summarize messages into a compact rolling summary.

    If existing_summary is provided, it's included so the new summary
    incorporates everything before it.
    """
    parts: list[str] = []
    if existing_summary:
        parts.append(f"[Previous summary]\n{existing_summary}\n")
    parts.append("[Recent messages to summarize]")
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        # Truncate very long messages (e.g. tool outputs)
        if len(content) > MESSAGE_CHAR_LIMIT:
            head = MESSAGE_CHAR_LIMIT * 2 // 3
            content = content[:head] + "\n...(truncated)...\n" + content[-(MESSAGE_CHAR_LIMIT - head) :]
        parts.append(f"{role}: {content}")

    text = "\n".join(parts)
    # Cap input to summarization call
    if len(text) > TRANSCRIPT_CHAR_LIMIT:
        half = TRANSCRIPT_CHAR_LIMIT // 2
        text = text[:half] + "\n...\n" + text[-(TRANSCRIPT_CHAR_LIMIT - half) :]

    try:
        from cptr.utils.ai import generate_text
        from cptr.utils.utility_models import configured_utility_model

        model_id = await configured_utility_model("summary_generation")

        async def attempt(budget: int, extra: str = ""):
            return await generate_text(
                None,
                model_id=model_id,
                active_connection=connection,
                active_model=model,
                messages=[{"role": "user", "content": text}],
                system=_get_summarize_prompt() + extra,
                max_tokens=budget,
            )

        result = await attempt(SUMMARY_MAX_TOKENS)
        if result is not None and getattr(result, "finish_reason", None) == "length":
            logger.warning("[summarize] summary hit %d tokens; retrying with %d", SUMMARY_MAX_TOKENS, SUMMARY_RETRY_MAX_TOKENS)
            retry = await attempt(
                SUMMARY_RETRY_MAX_TOKENS,
                "\n\nYour previous summary ran past its length limit. Be more concise: keep every decision, file "
                "path, open task and user instruction, and drop narration.",
            )
            if retry is not None and str(retry).strip():
                result = retry
            if getattr(result, "finish_reason", None) == "length" and str(result).strip():
                logger.warning("[summarize] summary still cut off at its limit; keeping it with a note")
                result = str(result).rstrip() + CUT_OFF_NOTE
        if result is None or not str(result).strip():
            raise RuntimeError("summary generation returned no text")
        result = str(result)
        logger.info("[summarize] LLM summary: %d chars", len(result))
        return result
    except Exception:
        logger.warning("[summarize] LLM call failed, using naive fallback", exc_info=True)
        return _naive_summary(messages, existing_summary)


def _naive_summary(messages: list[dict], existing: str | None) -> str:
    """Fallback when LLM summarization fails."""
    parts: list[str] = []
    if existing:
        parts.append(existing)
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, str) and role in ("user", "assistant"):
            parts.append(f"- {role}: {content[:200]}")
    return "\n".join(parts)[:2000]
