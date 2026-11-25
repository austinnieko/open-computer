from cptr.utils.context import compaction_keep_count, estimate_messages_tokens


def _m(role, chars, reasoning=0):
    m = {"role": role, "content": "x" * chars}
    if reasoning:
        m["reasoning_items"] = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "r" * reasoning}]}]
    return m


def test_reasoning_counts_toward_the_estimate():
    assert estimate_messages_tokens([_m("assistant", 0, reasoning=4000)]) >= 1000


def test_keep_is_capped_by_tokens_not_only_by_count():
    # 100 messages of ~10K tokens each; 40% by count would keep 40 (~400K tokens).
    msgs = [_m("assistant", 40_000) for _ in range(100)]
    keep = compaction_keep_count(msgs, 80_000)
    assert keep < 40
    assert estimate_messages_tokens(msgs[-keep:]) <= 80_000 * 2 // 5 + 10_010


def test_small_messages_still_keep_forty_percent():
    msgs = [_m("user", 40) for _ in range(50)]
    assert compaction_keep_count(msgs, 80_000) == 20


def test_always_keeps_at_least_two():
    msgs = [_m("assistant", 2_000_000) for _ in range(5)]
    assert compaction_keep_count(msgs, 80_000) == 2


def test_checkpoint_is_where_the_keep_zone_starts():
    from cptr.utils.chat_task import _summary_checkpoint_message_id
    keep = [{"role": "assistant", "id": "a2"}, {"role": "tool", "id": "a2"}, {"role": "user", "id": "uN"}]
    assert _summary_checkpoint_message_id(keep, "fb") == "a2"
