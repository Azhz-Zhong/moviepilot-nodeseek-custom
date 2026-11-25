"""Offline transport and account lifecycle checks; all credentials are synthetic."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import requests

ROOT = Path(__file__).resolve().parents[1] / "plugins.v3" / "nodeseeksigncustom"
package = types.ModuleType("ns_test_package")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
for name in ("client", "browser"):
    spec = importlib.util.spec_from_file_location(f"{package.__name__}.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
client = sys.modules["ns_test_package.client"]
browser = sys.modules["ns_test_package.browser"]


class Response:
    def __init__(self, code, body, headers=None):
        self.status_code = code
        self.headers = headers or {"Content-Type": "application/json"}
        self.body = json.dumps(body).encode()
    def iter_content(self, *args, **kwargs):
        yield self.body
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.cookies = types.SimpleNamespace(jar=requests.cookies.RequestsCookieJar())
        self.cookies.set = self.cookies.jar.set
    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(self)
        return response
    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


def logged_in(session):
    session.cookies.set("session", "renewed-fixture", domain="www.nodeseek.com", path="/")
    session.cookies.set("pjwt", "renewed-jwt", domain="www.nodeseek.com", path="/")
    return Response(200, {"success": True})


class LifecycleTests(unittest.TestCase):
    def execute(self, responses, cookie="session=old-fixture", **kwargs):
        session = Session(responses)
        saved, pending = [], []
        curl = types.ModuleType("curl_cffi")
        curl.requests = types.SimpleNamespace(Session=Mock(return_value=session),
            exceptions=types.SimpleNamespace(RequestException=requests.RequestException))
        def solve(endpoint, proxy_url=None, **options):
            if options.get("mode") in {"turnstile-min", "turnstile-max"}:
                return "synthetic-token"
            return browser.BrowserSession({"user-agent": "test-browser"}, {"cf_clearance": "synthetic-cf"}, 10)
        with patch.dict(sys.modules, {"curl_cffi": curl}), patch.object(browser, "create_browser_session", side_effect=solve):
            result = client.submit_attendance(cookie, False, browser_service="http://browser.test",
                credentials={"username": "fixture-user", "password": "fixture-password", "email": "fixture@example.test"},
                save_session=saved.append, save_pending=pending.append, **kwargs)
        return result, session.calls, saved, pending

    def test_valid_cookie_never_logs_in(self):
        result, calls, saved, _ = self.execute([Response(200, {"record": None}), Response(200, {"success": True, "gain": 5})])
        self.assertEqual((result.status, result.gain, len(calls), saved), ("success", 5, 2, []))

    def test_expiry_logs_in_once_then_saves_before_attendance(self):
        result, calls, saved, _ = self.execute([
            Response(401, {}), logged_in, Response(200, {"record": None}), Response(200, {"success": True, "gain": 6})])
        self.assertEqual(result.status, "success")
        self.assertEqual(len(calls), 4)
        self.assertEqual(saved, ["session=renewed-fixture; pjwt=renewed-jwt"])
        self.assertNotIn("x-captcha-token", calls[-1][1]["headers"])

    def test_missing_cookie_logs_in_then_attends(self):
        result, calls, saved, _ = self.execute([logged_in, Response(200, {"record": None}), Response(200, {"success": True})], cookie="")
        self.assertEqual(result.status, "success")
        self.assertEqual(len(calls), 3)
        self.assertTrue(saved)

    def test_email_redirect_requests_code_without_saving_partial_session(self):
        result, calls, saved, pending = self.execute([
            Response(401, {}), Response(200, {"success": True, "redirect": "/emailSignIn.html?email=fixture%40example.test"}),
            Response(200, {"success": True})])
        self.assertEqual(result.status, "email_required")
        self.assertEqual(saved, [])
        self.assertEqual(pending[-1]["email"], "fixture@example.test")
        self.assertTrue(calls[-1][0].endswith("/api/email"))

    def test_email_code_saves_session_and_continues(self):
        result, calls, saved, pending = self.execute([logged_in, Response(200, {"record": None}), Response(200, {"success": True, "gain": 3})],
            email_code="123456", pending_login={"email": "fixture@example.test"})
        self.assertEqual(result.status, "success")
        self.assertTrue(calls[0][0].endswith("/api/account/emailSignIn"))
        self.assertTrue(saved)
        self.assertEqual(pending, [{}])

    def test_bad_code_does_not_attend_or_overwrite_cookie(self):
        result, calls, saved, _ = self.execute([Response(200, {"success": False, "message": "验证码错误"})], email_code="000000")
        self.assertEqual(result.status, "email_required")
        self.assertEqual((len(calls), saved), (1, []))

    def test_invalid_code_is_not_submitted(self):
        result, calls, saved, _ = self.execute([], email_code="invalid value")
        self.assertEqual((result.status, calls, saved), ("email_required", [], []))

    def test_ambiguous_attendance_never_retries(self):
        result, calls, saved, _ = self.execute([requests.ConnectionError("synthetic secret url")])
        self.assertEqual((result.status, len(calls), saved), ("uncertain", 1, []))
        self.assertNotIn("synthetic secret", str(result))

    def test_two_factor_does_not_save_or_attend(self):
        result, calls, saved, _ = self.execute([Response(200, {"success": True, "need2FA": True, "otpSession": "fixture"})], cookie="")
        self.assertEqual((result.status, len(calls), saved), ("login_failed", 1, []))

    def test_cf_challenge_is_not_cookie_expiry(self):
        result, calls, saved, _ = self.execute([Response(403, {}, {"cf-mitigated": "challenge"})])
        self.assertEqual((result.status, len(calls), saved), ("verification", 1, []))

    def test_cookie_normalization(self):
        self.assertEqual(client.normalize_cookie("Cookie: session=fixture; cf_clearance=discard; smac=signed"), "session=fixture; smac=signed")
        with self.assertRaises(ValueError):
            client.normalize_cookie("smac=not-a-session")

    def test_existing_daily_record_never_posts_attendance(self):
        from datetime import datetime, timezone
        result, calls, saved, _ = self.execute([Response(200, {"record": {"created_at": datetime.now(timezone.utc).isoformat(), "gain": 5}})])
        self.assertEqual((result.status, result.gain, len(calls), saved), ("already", 5, 1, []))

    def test_duplicate_reported_as_http_500_is_recognized(self):
        result, calls, _, _ = self.execute([Response(200, {"record": None}), Response(500, {"success": False, "message": "今天已经签到"})])
        self.assertEqual((result.status, len(calls)), ("already", 2))

    def test_verify_only_never_posts_when_record_missing(self):
        result, calls, _, _ = self.execute([Response(200, {"record": None})], verify_only=True)
        self.assertEqual((result.status, len(calls)), ("uncertain", 1))

    def test_pending_email_cannot_override_current_account(self):
        result, calls, saved, _ = self.execute([], email_code="123456", pending_login={"email": "old@example.test"})
        self.assertEqual((result.status, calls, saved), ("config_error", [], []))

    def test_service_unready_is_sanitized(self):
        fake = Session([Response(500, {"code": 500, "message": "The scanner is not ready yet fixture-secret"})])
        with patch.object(browser.requests, "Session", return_value=fake):
            with self.assertRaises(browser.BrowserServiceError) as caught:
                browser.create_browser_session("http://browser.test")
        result = caught.exception.result
        self.assertEqual(result.status, "service_error")
        self.assertEqual(result.service_http, 500)
        self.assertNotIn("fixture-secret", str(result))


if __name__ == "__main__":
    unittest.main()
