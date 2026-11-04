"""gh auth status fallback for GitHub CLI versions without ``--json`` (e.g. Ubuntu's 2.45).

Run: uv run python -m unittest tests.test_gh_auth_status -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from cptr.utils import gh

MULTI_ACCOUNT = """github.com
  ✓ Logged in to github.com account octo (keyring)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_************************************
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'

  ✓ Logged in to github.com account other (keyring)
  - Active account: false
  - Git operations protocol: ssh
  - Token: gho_************************************
  - Token scopes: 'repo'
"""

OLD_FORMAT = """github.com
  ✓ Logged in to github.com as octo (/home/u/.config/gh/hosts.yml)
  ✓ Git operations for github.com configured to use https protocol.
  ✓ Token: *******************
"""

FAILED = """github.com
  X Failed to log in to github.com account octo (keyring)
  - Active account: true
  - The token in keyring is invalid.
"""


class ParseTests(unittest.TestCase):
    def test_multi_account(self):
        hosts = gh.parse_auth_status_text(MULTI_ACCOUNT)
        accounts = hosts["github.com"]
        self.assertEqual([a["login"] for a in accounts], ["octo", "other"])
        self.assertEqual([a["active"] for a in accounts], [True, False])
        self.assertEqual(accounts[0]["state"], "success")
        self.assertEqual(accounts[0]["gitProtocol"], "https")
        self.assertEqual(accounts[1]["gitProtocol"], "ssh")
        self.assertEqual(accounts[0]["scopes"], "gist, read:org, repo, workflow")
        self.assertEqual(accounts[0]["tokenSource"], "keyring")
        self.assertNotIn("token", accounts[0])

    def test_old_single_account_format(self):
        account = gh.parse_auth_status_text(OLD_FORMAT)["github.com"][0]
        self.assertEqual(account["login"], "octo")
        self.assertTrue(account["active"])
        self.assertEqual(account["gitProtocol"], "https")

    def test_failed_login_is_error_state(self):
        account = gh.parse_auth_status_text(FAILED)["github.com"][0]
        self.assertEqual(account["state"], "error")

    def test_not_logged_in(self):
        text = "You are not logged into any GitHub hosts. To log in, run: gh auth login\n"
        self.assertEqual(gh.parse_auth_status_text(text), {})


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_status_falls_back_when_json_flag_is_unknown(self):
        calls = []

        async def fake_run_gh(args, **kwargs):
            calls.append(args)
            if "--json" in args:
                return 1, "", "unknown flag: --json\n\nUsage:  gh auth status [flags]\n"
            return 0, "", MULTI_ACCOUNT  # gh 2.45 prints status to stderr

        with (
            mock.patch.object(gh, "version", mock.AsyncMock(return_value="gh version 2.45.0")),
            mock.patch.object(gh, "run_gh", fake_run_gh),
        ):
            status = await gh.auth_status(mock.Mock())
        self.assertEqual(calls[1], ["auth", "status"])
        self.assertEqual(status["hosts"]["github.com"][0]["login"], "octo")
        self.assertNotIn("message", status)

    async def test_fallback_not_signed_in_keeps_message(self):
        async def fake_run_gh(args, **kwargs):
            if "--json" in args:
                return 1, "", "unknown flag: --json"
            return 1, "", "You are not logged into any GitHub hosts. To log in, run: gh auth login"

        with (
            mock.patch.object(gh, "version", mock.AsyncMock(return_value="gh version 2.45.0")),
            mock.patch.object(gh, "run_gh", fake_run_gh),
        ):
            status = await gh.auth_status(mock.Mock())
        self.assertEqual(status["hosts"], {})
        self.assertIn("not logged into", status["message"])


if __name__ == "__main__":
    unittest.main()
