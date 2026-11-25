"""Optional user-managed browser service; endpoints and sessions stay private."""

from dataclasses import dataclass
import re
from time import perf_counter
from urllib.parse import unquote, urlsplit

import requests

from .client import AttendanceResult, COOKIE_VALUE, FORUM_URL, MAX_RESPONSE


class BrowserServiceError(Exception):
    def __init__(self, result):
        self.result = result


def normalize_service_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value.strip()):
        raise ValueError("验证服务地址格式无效。")
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("请填写 http 或 https 验证服务地址，不要包含账号密码或查询参数。")
    if not value.endswith("/cf-clearance-scraper"):
        value += "/cf-clearance-scraper"
    return value


@dataclass
class BrowserSession:
    headers: dict
    cookies: dict
    elapsed_ms: int


def create_browser_session(endpoint, proxy_url=None, *, mode="waf-session", page=""):
    """Ask the user's service for one fresh session on the same egress route."""
    started = perf_counter()
    http_status = None

    def fail(reason):
        raise BrowserServiceError(AttendanceResult(
            "service_error", "浏览器验证服务未完成验证，本次尚未提交签到。",
            reason=reason, stage="浏览器验证", service_http=http_status,
            service_ms=round((perf_counter() - started) * 1000)))

    try:
        endpoint = normalize_service_url(endpoint)
    except ValueError:
        fail("验证服务地址无效")
    if mode not in {"waf-session", "turnstile-min", "turnstile-max"} or page not in {"", "signIn.html", "emailSignIn.html"}:
        fail("验证请求配置无效")
    payload = {"url": FORUM_URL + page, "mode": mode}
    if mode == "turnstile-min":
        # Public site key used by NodeSeek's own login/email form (not a credential).
        payload["siteKey"] = "0x4AAAAAAAaNy7leGjewpVyR"
    if proxy_url:
        try:
            proxy = urlsplit(proxy_url)
            if proxy.scheme not in {"http", "https"} or not proxy.hostname:
                fail("验证服务需要 HTTP 系统代理")
            payload["proxy"] = {"host": proxy.hostname, "port": proxy.port or (443 if proxy.scheme == "https" else 80)}
            if proxy.username:
                payload["proxy"]["username"] = unquote(proxy.username)
            if proxy.password:
                payload["proxy"]["password"] = unquote(proxy.password)
        except ValueError:
            fail("系统代理地址无效")
    try:
        # The LAN service connection must not be routed through the outbound proxy.
        with requests.Session() as session:
            session.trust_env = False
            with session.post(endpoint, json=payload, timeout=(6, 75), allow_redirects=False, stream=True) as reply:
                http_status = reply.status_code
                body = bytearray()
                for chunk in reply.iter_content(8192):
                    if len(body) + len(chunk) > MAX_RESPONSE:
                        fail("验证服务响应超出限制")
                    body.extend(chunk)
                try:
                    import json
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    fail("验证服务返回非 JSON")
    except requests.RequestException as error:
        fail("验证服务连接失败：" + type(error).__name__)
    if not isinstance(data, dict):
        fail("验证服务响应格式无效")
    if http_status != 200 or data.get("code") != 200:
        # Never log arbitrary upstream errors: they can contain URLs or tokens.
        message = str(data.get("message", "")).lower()
        reason = "验证服务拒绝请求"
        if "not ready" in message:
            reason = "验证服务扫描器未就绪"
        elif "timeout" in message or "timed out" in message:
            reason = "浏览器验证超时"
        fail(reason)
    if mode in {"turnstile-min", "turnstile-max"}:
        token = data.get("token")
        if not isinstance(token, str) or not token or len(token) > 16384 or re.search(r"[\r\n\x00]", token):
            fail("验证服务未返回登录验证令牌")
        return token
    raw_headers, raw_cookies = data.get("headers"), data.get("cookies")
    if not isinstance(raw_headers, dict) or not isinstance(raw_cookies, list):
        fail("验证服务未返回有效会话")
    allowed = {"user-agent", "accept-language", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}
    headers = {k.lower(): v for k, v in raw_headers.items() if isinstance(k, str) and k.lower() in allowed
               and isinstance(v, str) and len(v) < 2048 and not re.search(r"[\r\n\x00]", v)}
    cookies = {}
    for item in raw_cookies:
        if not isinstance(item, dict) or item.get("name") != "cf_clearance":
            continue
        domain, value = item.get("domain", ""), item.get("value", "")
        if isinstance(domain, str) and domain.lstrip(".") in {"nodeseek.com", "www.nodeseek.com"}:
            if isinstance(value, str) and COOKIE_VALUE.fullmatch(value):
                cookies["cf_clearance"] = value
    if not headers.get("user-agent") or not cookies.get("cf_clearance"):
        fail("验证服务未返回 NodeSeek 的有效验证会话")
    return BrowserSession(headers, cookies, round((perf_counter() - started) * 1000))
