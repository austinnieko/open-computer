from cptr.utils.tools import tool_server_prompt_text


def _tools(sid, names):
    return {f"{sid}_{n}": {"server": {"id": sid}, "original_name": n} for n in names}


def test_same_service_servers_are_grouped():
    servers = [
        {"id": "linear", "name": "Linear", "description": "Workspace BarkYard Labs"},
        {"id": "aa_linear", "name": "Linear", "description": "Workspace application-automation"},
        {"id": "ot", "name": "Open Terminal", "description": ""},
        {"id": "off", "name": "Disabled"},
    ]
    tools = {**_tools("linear", ["get_issue", "list_issues"]),
             **_tools("aa_linear", ["get_issue", "list_issues"]),
             **_tools("ot", ["run_command"])}
    text = tool_server_prompt_text(servers, tools)
    assert "- `linear_*`: Linear - Workspace BarkYard Labs" in text
    assert "- `aa_linear_*`: Linear - Workspace application-automation" in text
    assert "- `ot_*`: Open Terminal" in text
    assert "off_" not in text
    assert "`linear_*`, `aa_linear_*` are the same service" in text
    assert "`ot_*`, " not in text


def test_no_servers():
    assert tool_server_prompt_text([], {}) == ""


if __name__ == "__main__":
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        fn()
    print("ok")
