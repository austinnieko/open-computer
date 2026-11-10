from cptr.utils.leaked_tool_calls import extract_leaked_tool_call


def test_glm_style_call_at_end():
    text = (
        "Now implementing:\n\n<tool_call>ot_replace_file<arg_key>path</arg_key>"
        "<arg_value>/home/user/a.py</arg_value><arg_key>replacements</arg_key>"
        '<arg_value>[{"old_string": "a", "new_string": "b"}]</arg_value></tool_call>\n'
    )
    start, name, args = extract_leaked_tool_call(text)
    assert text[:start] == "Now implementing:\n\n"
    assert name == "ot_replace_file"
    assert args == {
        "path": "/home/user/a.py",
        "replacements": [{"old_string": "a", "new_string": "b"}],
    }


def test_unterminated_call():
    start, name, args = extract_leaked_tool_call("x <tool_call>ot_list_process_status")
    assert (start, name, args) == (2, "ot_list_process_status", {})


def test_truncated_arguments_are_not_recovered():
    text = "<tool_call>ot_run_command<arg_key>command</arg_key><arg_value>rm -rf /tmp/x"
    assert extract_leaked_tool_call(text) is None


def test_json_style_call():
    text = '<tool_call>{"name": "bash", "arguments": {"command": "ls"}}</tool_call>'
    assert extract_leaked_tool_call(text) == (0, "bash", {"command": "ls"})


def test_prose_after_call_is_ignored():
    text = "Use <tool_call>name</tool_call> syntax, then continue."
    assert extract_leaked_tool_call(text) is None


def test_plain_text_is_ignored():
    assert extract_leaked_tool_call("All done.") is None
    assert extract_leaked_tool_call("") is None


if __name__ == "__main__":
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        fn()
    print("ok")
