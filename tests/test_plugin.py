"""Runtime checks run inside MoviePilot; isolated data and mocked network/jobs."""
from contextlib import ExitStack
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from app.sdk.plugin import _PluginBase
except ImportError:
    raise unittest.SkipTest("MoviePilot SDK is required for runtime checks")

root = Path(__file__).resolve().parents[1] / "plugins.v3" / "nodeseeksigncustom"
spec = importlib.util.spec_from_file_location("ns_test_runtime", root / "__init__.py", submodule_search_locations=[str(root)])
code = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = code
spec.loader.exec_module(code)


class Isolated(code.NodeSeekSignCustom):
    def get_data_path(self):
        return self.path
    def get_data(self, key):
        return self.data.get(key)
    def save_data(self, key, value):
        self.data[key] = value
    def update_config(self, value):
        self.config = value
    def post_message(self, **kwargs):
        self.messages.append(kwargs)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(_PluginBase, "__init__", return_value=None))
        self.stack.enter_context(patch.object(code.scheduler, "add_plugin_once_job", return_value=True))
        self.stack.enter_context(patch.object(code, "logger"))
        self.p = Isolated()
        self.p.path, self.p.data, self.p.messages = Path(folder), {}, []
        self.p.init_plugin({"auto_enabled": True, "use_proxy": False, "notify": False,
                            "session_cookie": "session=fixture", "browser_service_url": "http://browser.test",
                            "account_username": "fixture@example.test", "account_password": "fixture-password"})

    def test_private_inputs_are_cleared_and_owner_only(self):
        self.assertFalse(set(self.p.config) & {"session_cookie", "account_username", "account_password", "account_email", "browser_service_url"})
        for name in ("account.json", "browser-service.json", "session.cookie"):
            self.assertEqual((self.p.path / name).stat().st_mode & 0o077, 0)
        self.assertNotIn("http://browser.test", str(self.p.get_page()))

    def test_email_wait_notifies_once_and_preserves_schedule(self):
        with patch.object(code, "submit_attendance", return_value=code.AttendanceResult("email_required", "waiting")) as transport:
            self.p.run_attendance()
            self.p.run_attendance()
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(len(self.p.messages), 1)
        self.assertTrue(self.p.get_state())
        self.assertEqual(len(self.p.get_service()), 1)

    def test_email_code_resumes_even_if_already_signed_today(self):
        self.p._append(code.AttendanceResult("already", "already", 5), "test")
        self.p.save_data(code.GATE_KEY, "email_required")
        self.p.init_plugin({**self.p.config, "email_code": "123456"})
        with patch.object(code, "submit_attendance", return_value=code.AttendanceResult("already", "done", 5)) as transport:
            self.p.run_attendance("手动执行")
            self.p.run_attendance()
        self.assertEqual(transport.call_count, 1)
        self.assertTrue(transport.call_args.kwargs["verify_only"])
        self.assertEqual(transport.call_args.kwargs["email_code"], "123456")
        self.assertFalse(self.p._email_code)
        self.assertFalse(self.p.get_data(code.GATE_KEY))
        self.assertNotIn("email_code", self.p.config)

    def test_unknown_prior_attempt_can_only_be_reconciled(self):
        self.p._append(code.AttendanceResult("uncertain", "unknown"), "test")
        with patch.object(code, "submit_attendance", return_value=code.AttendanceResult("already", "done", 5)) as transport:
            self.p.run_attendance("手动执行")
        self.assertTrue(transport.call_args.kwargs["verify_only"])
        self.assertIn("5 鸡腿", str(self.p.get_page()))


if __name__ == "__main__":
    unittest.main()
