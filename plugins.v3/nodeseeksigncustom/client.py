"""Submit attendance using a normal, already-authenticated NodeSeek session."""

from dataclasses import dataclass
import json
import re
import requests


FORUM_URL = "https://www.nodeseek.com/"
USER_AGENT = "MoviePilot-NodeSeekSignCustom/0.3.0"
SESSION_KEYS = ("session", "pjwt")
COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}\Z")
MAX_RESPONSE = 64 * 1024


@dataclass(frozen=True)
class AttendanceResult:
    status: str
    message: str
    gain: int | None = None


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


def _blocked(code, headers):
    if (headers.get("cf-mitigated") or "").casefold() == "challenge":
        return AttendanceResult("verification", "站点要求浏览器验证，自动签到已暂停。")
    if code in (401, 403) or 300 <= code < 400:
        return AttendanceResult("session_required", "会话未获授权，请在浏览器登录后更新 Cookie。")
    if code == 407:
        return AttendanceResult("proxy_error", "系统代理需要有效认证，请检查 MoviePilot 网络设置。")
    if code == 429:
        return AttendanceResult("rate_limited", "站点限制当前请求，下次计划时间再检查。")
    if code >= 500:
        return AttendanceResult("uncertain", f"站点返回 HTTP {code}，请在浏览器核对今天的结果。")
    if code != 200:
        return AttendanceResult("failed", f"签到请求未成功，HTTP {code}。")
    if "text/html" in (headers.get("Content-Type") or "").lower():
        return AttendanceResult("verification", "站点返回浏览器页面，请在浏览器检查登录或验证。")
    return None


def interpret_result(body: bytes) -> AttendanceResult:
    try:
        payload = json.loads(body)
    except (UnicodeError, ValueError):
        return AttendanceResult("uncertain", "站点响应无法识别，请在浏览器核对今天的结果。")
    if not isinstance(payload, dict):
        return AttendanceResult("uncertain", "站点响应无法识别，请在浏览器核对今天的结果。")
    if payload.get("success") is True:
        gain = payload.get("gain")
        gain = gain if type(gain) is int and gain >= 0 else None
        return AttendanceResult("success", "签到成功。", gain)
    message = payload.get("message")
    if isinstance(message, str):
        if any(word in message for word in ("已签到", "已完成签到", "已经签到", "重复签到", "已经签过")):
            return AttendanceResult("already", "站点确认今天已签到。")
        if any(word in message for word in ("登录", "登入", "未认证")):
            return AttendanceResult("session_required", "会话未获授权，请在浏览器登录后更新 Cookie。")
        if any(word in message for word in ("验证", "验证码")):
            return AttendanceResult("verification", "站点要求浏览器验证，自动签到已暂停。")
    return AttendanceResult("failed", "站点未确认签到，请在论坛页面查看。")


def submit_attendance(
    cookie: str, random_reward: bool, *, proxy_url: str | None = None
) -> AttendanceResult:
    """Send exactly one HTTPS POST using the selected network route."""
    headers = {"Cookie": cookie, "Accept": "application/json", "User-Agent": USER_AGENT}
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
                blocked = _blocked(response.status_code, response.headers)
                if blocked is not None:
                    return blocked
                body = bytearray()
                for chunk in response.iter_content(chunk_size=8192):
                    if len(body) + len(chunk) > MAX_RESPONSE:
                        return AttendanceResult("uncertain", "站点响应超出预期，请在浏览器核对今天的结果。")
                    body.extend(chunk)
    except (requests.exceptions.ProxyError, requests.exceptions.InvalidProxyURL, requests.exceptions.InvalidSchema):
        return AttendanceResult("proxy_error", "无法连接系统代理，请检查 MoviePilot 网络设置。")
    except requests.exceptions.ConnectTimeout:
        status = "proxy_error" if proxy_url else "connect_error"
        return AttendanceResult(status, "连接超时，请检查当前网络或系统代理。")
    except requests.exceptions.RequestException:
        return AttendanceResult("uncertain", "连接中断，签到结果未知。请在浏览器核对今天的结果。")
    return interpret_result(body)
