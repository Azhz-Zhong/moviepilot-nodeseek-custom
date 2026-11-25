"""Submit attendance using a normal, already-authenticated NodeSeek session."""

from dataclasses import dataclass, replace
import json
import re
from time import perf_counter
from urllib.parse import urlsplit

import requests


FORUM_URL = "https://www.nodeseek.com/"
USER_AGENT = "MoviePilot-NodeSeekSignCustom/0.3.2"
SESSION_KEYS = ("session", "pjwt")
COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}\Z")
MAX_RESPONSE = 64 * 1024


@dataclass(frozen=True)
class AttendanceResult:
    status: str
    message: str
    gain: int | None = None
    http_status: int | None = None
    elapsed_ms: int | None = None
    response_type: str | None = None
    reason: str | None = None


def normalize_cookie(header: str) -> str:
    """Retain only the first-party account session keys."""
    header = header.strip()
    if len(header) > 16_384 or any(char in header for char in ("\r", "\n", "\x00")):
        raise ValueError("Cookie 格式无效，请粘贴浏览器复制的单行 Cookie。")
    if header[:7].lower() == "cookie:":
        header = header[7:].strip()
    cookies = {}
    for item in header.split(";"):
        name, separator, value = item.strip().partition("=")
        if name in SESSION_KEYS:
            if not separator or not COOKIE_VALUE.fullmatch(value) or name in cookies:
                raise ValueError("会话 Cookie 格式无效，请重新复制。")
            cookies[name] = value
    if not cookies:
        raise ValueError("未找到登录后的 session 或 pjwt。")
    return "; ".join(f"{name}={cookies[name]}" for name in SESSION_KEYS if name in cookies)


def _login_redirect(headers) -> bool:
    try:
        target = urlsplit(headers.get("Location") or "")
        return (
            target.hostname in (None, "www.nodeseek.com", "nodeseek.com")
            and target.path.lower() in ("/signin.html", "/emailsignin.html")
        )
    except ValueError:
        return False


def _response_type(headers) -> str:
    media_type = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    return {"application/json": "JSON", "text/html": "HTML", "text/plain": "纯文本"}.get(media_type, "其他")


def _blocked(code, headers):
    if (headers.get("cf-mitigated") or "").casefold() == "challenge":
        return AttendanceResult("verification", "站点要求浏览器验证，自动签到已暂停。", reason="站点验证页")
    if code == 401 or (300 <= code < 400 and _login_redirect(headers)):
        return AttendanceResult("session_required", "站点要求登录，请检查并更新会话 Cookie。",
                                reason="登录跳转" if code != 401 else "未认证")
    if code == 403 or 300 <= code < 400:
        return AttendanceResult("forbidden", "站点拒绝请求或跳转到其他页面，暂时无法确认会话状态。",
                                reason="访问被拒绝" if code == 403 else "非登录跳转")
    if code == 407:
        return AttendanceResult("proxy_error", "系统代理需要有效认证，请检查 MoviePilot 网络设置。", reason="代理认证")
    if code == 429:
        return AttendanceResult("rate_limited", "站点限制当前请求，下次计划时间再检查。", reason="限流")
    if code >= 500:
        return AttendanceResult("uncertain", f"站点返回 HTTP {code}，请在浏览器核对今天的结果。", reason="上游服务错误")
    if code != 200:
        return AttendanceResult("failed", f"签到请求未成功，HTTP {code}。", reason="异常 HTTP 响应")
    if _response_type(headers) == "HTML":
        return AttendanceResult("uncertain", "站点返回网页，无法确认签到结果。", reason="预期 JSON，实际 HTML")
    return None


def interpret_result(body: bytes) -> AttendanceResult:
    try:
        payload = json.loads(body)
    except (UnicodeError, ValueError):
        return AttendanceResult("uncertain", "站点响应无法识别，请在浏览器核对今天的结果。", reason="无法解析签到响应")
    if not isinstance(payload, dict):
        return AttendanceResult("uncertain", "站点响应无法识别，请在浏览器核对今天的结果。", reason="无法解析签到响应")
    if payload.get("success") is True:
        gain = payload.get("gain")
        gain = gain if type(gain) is int and gain >= 0 else None
        return AttendanceResult("success", "签到成功。", gain, reason="站点确认成功")
    message = payload.get("message")
    if isinstance(message, str):
        if any(word in message for word in ("已签到", "已完成签到", "已经签到", "重复签到", "已经签过")):
            return AttendanceResult("already", "站点确认今天已签到。", reason="站点确认已签到")
        if any(word in message for word in ("登录已过期", "登录过期", "会话已过期", "会话过期", "登录状态已失效", "Cookie 已过期")):
            return AttendanceResult("session_expired", "站点报告 Cookie 已过期，请更新会话。", reason="站点确认会话过期")
        if any(word in message for word in ("未登录", "尚未登录", "请登录", "请先登录", "请重新登录", "未登入", "未认证")):
            return AttendanceResult("session_required", "站点要求登录，请检查并更新会话 Cookie。", reason="站点要求登录")
        if any(word in message for word in ("验证", "验证码")):
            return AttendanceResult("verification", "站点要求浏览器验证，自动签到已暂停。", reason="签到接口要求验证")
    return AttendanceResult("failed", "站点未确认签到，请在论坛页面查看。", reason="签到接口拒绝")


def submit_attendance(
    cookie: str, random_reward: bool, *, proxy_url: str | None = None
) -> AttendanceResult:
    """Send exactly one HTTPS POST using the selected network route."""
    headers = {"Cookie": cookie, "Accept": "application/json", "User-Agent": USER_AGENT}
    started = perf_counter()
    http_status = None
    response_type = None

    def finish(result):
        return replace(result, http_status=http_status, response_type=response_type,
                       elapsed_ms=round((perf_counter() - started) * 1000))

    try:
        with requests.Session() as session:
            session.trust_env = False
            proxies = {"https": proxy_url} if proxy_url else {}
            with session.post(
                FORUM_URL + "api/attendance",
                params={"random": str(random_reward).lower()},
                headers=headers,
                timeout=(6, 12),
                proxies=proxies,
                allow_redirects=False,
                stream=True,
            ) as response:
                http_status = response.status_code
                response_type = _response_type(response.headers)
                blocked = _blocked(http_status, response.headers)
                if blocked is not None:
                    return finish(blocked)
                body = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if len(body) + len(chunk) > MAX_RESPONSE:
                        return finish(AttendanceResult("uncertain", "站点响应超出预期，请在浏览器核对今天的结果。", reason="响应体超出限制"))
                    body.extend(chunk)
    except (requests.exceptions.ProxyError, requests.exceptions.InvalidProxyURL, requests.exceptions.InvalidSchema) as error:
        return finish(AttendanceResult("proxy_error", "无法连接系统代理，请检查 MoviePilot 网络设置。", reason=type(error).__name__))
    except requests.exceptions.ConnectTimeout:
        status = "proxy_error" if proxy_url else "connect_error"
        return finish(AttendanceResult(status, "连接超时，请检查当前网络或系统代理。", reason="ConnectTimeout"))
    except requests.exceptions.RequestException as error:
        return finish(AttendanceResult("uncertain", "连接中断，签到结果未知。请在浏览器核对今天的结果。", reason=type(error).__name__))
    return finish(interpret_result(body))
