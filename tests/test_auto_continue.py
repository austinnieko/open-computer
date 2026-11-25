from types import SimpleNamespace as M

from cptr.utils.chat_task import _merge_pending_input_meta, auto_continue_number


def _msg(id, role, parent=None, meta=None):
    return M(id=id, role=role, parent_id=parent, meta=meta, content="", model="m")


def test_first_stop_continues_once():
    u = _msg("u1", "user")
    a = _msg("a1", "assistant", "u1")
    assert auto_continue_number(a, [u, a], limit=3, subagent=False) == 1


def test_counts_up_through_the_auto_continue_chain_then_stops():
    u = _msg("u3", "user", "a2", {"auto_continue": 2})
    a = _msg("a3", "assistant", "u3")
    assert auto_continue_number(a, [u, a], limit=3, subagent=False) == 3
    u = _msg("u4", "user", "a3", {"auto_continue": 3})
    a = _msg("a4", "assistant", "u4")
    assert auto_continue_number(a, [u, a], limit=3, subagent=False) is None


def test_off_for_subagents_zero_limit_and_user_queued_message():
    u = _msg("u1", "user")
    a = _msg("a1", "assistant", "u1")
    assert auto_continue_number(a, [u, a], limit=3, subagent=True) is None
    assert auto_continue_number(a, [u, a], limit=0, subagent=False) is None
    q = _msg("q1", "user", "a1", {"queued": True})
    assert auto_continue_number(a, [u, a, q], limit=3, subagent=False) is None


def test_a_real_user_message_resets_the_count():
    u = _msg("u9", "user", "a8")  # typed by the user, no auto_continue
    a = _msg("a9", "assistant", "u9")
    assert auto_continue_number(a, [u, a], limit=3, subagent=False) == 1


def test_merge_keeps_auto_continue():
    batch = [_msg("q1", "user", "a1", {"queued": True, "auto_continue": 2})]
    assert _merge_pending_input_meta(batch) == {"auto_continue": 2}
    assert _merge_pending_input_meta([_msg("q2", "user", "a1", {"queued": True})]) is None
