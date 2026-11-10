import asyncio
import json

import httpx
import pytest

from cptr.utils import openapi
from cptr.utils.repeat_guard import RepeatGuard


def _ot_result(pid, chunks):
    return json.dumps({
        "id": pid,
        "command": "sleep 55; tail -c 8000 /tmp/o3.log",
        "status": "done",
        "exit_code": 0,
        "log_path": f"/logs/{pid}.jsonl",
        "output": [{"type": "output", "data": c} for c in chunks],
        "next_offset": len(chunks),
    })


def test_same_output_with_new_process_ids_counts_as_a_repeat():
    g = RepeatGuard(warn_after=3, limit=5)
    args = {"command": "sleep 55; tail -c 8000 /tmp/o3.log", "wait": "70"}
    first = g.record("ot_run_command", args, _ot_result("a1", ["F\r\n", "1\r\n"]))
    second = g.record("ot_run_command", args, _ot_result("b2", ["F", "\r\n", "1\r\n"]))
    assert "[Note:" not in first and "[Note:" not in second
    third = g.record("ot_run_command", args, _ot_result("c3", ["F\r\n1\r\n"]))
    assert "same result 3 times" in third
    assert g.blocked("ot_run_command", args) is None
    g.record("ot_run_command", args, _ot_result("d4", ["F\r\n1\r\n"]))
    g.record("ot_run_command", args, _ot_result("e5", ["F\r\n1\r\n"]))
    refusal = g.blocked("ot_run_command", args)
    assert refusal and refusal.startswith("Error: not run")


def test_changing_output_resets_the_streak():
    g = RepeatGuard(warn_after=2, limit=3)
    args = {"command": "tail build.log"}
    g.record("ot_run_command", args, _ot_result("a", ["step 1"]))
    g.record("ot_run_command", args, _ot_result("b", ["step 1"]))
    g.record("ot_run_command", args, _ot_result("c", ["step 1"]))
    assert g.blocked("ot_run_command", args)
    out = g.record("ot_run_command", args, _ot_result("d", ["step 2"]))
    assert "[Note:" not in out
    assert g.blocked("ot_run_command", args) is None


def test_a_refused_call_may_run_again_once():
    g = RepeatGuard(warn_after=2, limit=3)
    args = {"command": "git status"}
    for _ in range(3):
        g.record("ot_run_command", args, "clean")
    assert g.blocked("ot_run_command", args)
    assert g.blocked("ot_run_command", args) is None  # the retry runs
    out = g.record("ot_run_command", args, "1 file changed")
    assert "[Note:" not in out
    assert g.blocked("ot_run_command", args) is None
    g.record("ot_run_command", args, "1 file changed")
    g.record("ot_run_command", args, "1 file changed")
    assert g.blocked("ot_run_command", args)


def test_different_arguments_are_tracked_separately():
    g = RepeatGuard(warn_after=2, limit=2)
    g.record("read_file", {"path": "a"}, "same")
    g.record("read_file", {"path": "a"}, "same")
    assert g.blocked("read_file", {"path": "a"})
    assert g.blocked("read_file", {"path": "b"}) is None
    assert g.blocked("write_file", {"path": "a"}) is None


def test_argument_order_does_not_matter():
    g = RepeatGuard(warn_after=2, limit=2)
    g.record("t", {"a": 1, "b": 2}, "x")
    g.record("t", {"b": 2, "a": 1}, "x")
    assert g.blocked("t", {"a": 1, "b": 2})


@pytest.mark.parametrize(
    "args, expected",
    [
        ({"process_id": "p", "wait": "300"}, 330),
        ({"command": "x", "wait": 70}, 100),
        ({"command": "x", "wait": "5"}, 60),
        ({"command": "x"}, 60),
        ({"command": "x", "wait": "soon"}, 60),
        ({"command": "x", "wait": 99999}, 3630),
    ],
)
def test_openapi_timeout_covers_the_requested_wait(args, expected):
    assert openapi._timeout_for(args, 60) == expected


SPEC = {"paths": {"/execute/{process_id}/status": {"get": {
    "operationId": "get_process_status",
    "parameters": [{"name": "process_id", "in": "path"}, {"name": "wait", "in": "query"}],
}}}}


def test_execute_openapi_tool_uses_the_wait_based_timeout(monkeypatch):
    seen = {}
    real = httpx.AsyncClient

    def client(*a, **kw):
        seen["timeout"] = kw.get("timeout")
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))

    monkeypatch.setattr(openapi.httpx, "AsyncClient", client)
    out = asyncio.run(openapi.execute_openapi_tool(
        "http://ot", SPEC, "get_process_status", {"process_id": "p", "wait": "300"}
    ))
    assert json.loads(out) == {"ok": True}
    assert seen["timeout"] == 330
