"""config.toml round trips: strings with newlines/control characters, app_config sync, and a damaged file."""

import pathlib
import tempfile
import tomllib
import unittest
from unittest import mock

from cptr.utils import config as cfg

TRICKY = 'line one\nline two\twith "quotes", a back\\slash, a bell \x07, DEL \x7f, unicode é ✓ and \r\n end'


class ConfigTomlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.file = d / "config.toml"
        self.patches = [mock.patch.object(cfg, "CONFIG_FILE", self.file), mock.patch.object(cfg, "DATA_DIR", d)]
        for p in self.patches:
            p.start()
        cfg.invalidate_config_cache()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        cfg.invalidate_config_cache()
        self.tmp.cleanup()

    def test_tricky_strings_round_trip_through_a_valid_toml_file(self):
        cfg.save_config({"server": {"secret": "abc", "note": TRICKY, "tags": [TRICKY, "plain"]},
                         "app_config": {"dotted.key": TRICKY}})
        parsed = tomllib.loads(self.file.read_text())  # raises if the file is not valid TOML
        self.assertEqual(parsed["server"]["note"], TRICKY)
        self.assertEqual(parsed["server"]["tags"], [TRICKY, "plain"])
        self.assertEqual(parsed["app_config"]["dotted.key"], TRICKY)
        cfg.invalidate_config_cache()
        self.assertEqual(cfg.load_config()["server"]["note"], TRICKY)

    def test_app_config_with_multiline_prompts_survives_a_sync_and_reload(self):
        cfg.save_config({"server": {"secret": "keep-me"}})
        app = {"subagents.system_prompt": "# Rules\n\n- one\n- two \"quoted\"\n",
               "chat.models": {"m": {"params": {"system_prompt": "a\nb"}}},
               "chat.default_model": "glm-coder/GLM", "memory.enabled": True, "subagents.max_iterations": 30}
        cfg.sync_config_to_toml(app)
        self.assertEqual(cfg.load_app_config_from_toml(), app)
        cfg.invalidate_config_cache()
        self.assertEqual(cfg.load_config()["server"]["secret"], "keep-me")

    def test_a_damaged_file_keeps_its_jwt_secret(self):
        # What the old writer produced: a raw newline inside a basic string.
        self.file.write_text('[server]\nsecret = "keep-me"\n\n[app_config]\n"x.prompt" = "first\nsecond"\n')
        with self.assertRaises(tomllib.TOMLDecodeError):
            tomllib.loads(self.file.read_text())
        cfg.invalidate_config_cache()
        with self.assertLogs(cfg.log, level="ERROR"):
            self.assertEqual(cfg._get_jwt_secret(), "keep-me")

    def test_odd_keys_and_lone_surrogates_still_write_valid_toml(self):
        cfg.save_config({"server": {"secret": "s", "has space": "x", "path/like": "y", "bad": "a\ud800b"}})
        parsed = tomllib.loads(self.file.read_text())
        self.assertEqual(parsed["server"]["has space"], "x")
        self.assertEqual(parsed["server"]["path/like"], "y")
        self.assertEqual(parsed["server"]["secret"], "s")
        self.assertEqual(parsed["server"]["bad"], "a�b")

    def test_a_failed_write_leaves_the_old_file_and_no_temp_file(self):
        cfg.save_config({"server": {"secret": "keep-me"}})
        with mock.patch.object(cfg.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                cfg.save_config({"server": {"secret": "new"}})
        self.assertEqual(tomllib.loads(self.file.read_text())["server"]["secret"], "keep-me")
        self.assertEqual(sorted(x.name for x in self.file.parent.iterdir()), ["config.toml"])

    def test_a_failed_render_leaves_the_old_file(self):
        cfg.save_config({"server": {"secret": "keep-me"}})
        with mock.patch.object(cfg, "_toml_str", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                cfg.save_config({"server": {"secret": "new", "x": "y"}})
        self.assertEqual(tomllib.loads(self.file.read_text())["server"]["secret"], "keep-me")

    def test_simple_parser_reads_the_new_escapes(self):
        cfg.save_config({"server": {"secret": "s", "note": TRICKY}})
        self.assertEqual(cfg._parse_simple_toml(self.file.read_text())["server"]["note"], TRICKY)


if __name__ == "__main__":
    unittest.main()
