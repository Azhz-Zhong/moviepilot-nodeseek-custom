"""Submit attendance using a normal, already-authenticated NodeSeek session."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import re
from time import perf_counter
from urllib.parse import urlsplit, parse_qs
from time import time

import requests


FORUM_URL = "https://www.nodeseek.com/"
USER_AGENT = "MoviePilot-NodeSeekSignCustom/0.4.0"
SESSION_KEYS = ("session", "pjwt", "smac")
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
    stage: str | None = None
    service_http: int | None = None
    service_ms: int | None = None
    login_http: int | None = None


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
    if not any(name in cookies for name in ("session", "pjwt")):
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
        if any(word in message.lower() for word in ("email", "邮箱", "邮件")):
            return AttendanceResult("email_required", "NodeSeek 要求邮箱验证，请在浏览器完成验证后更新 Cookie。", reason="需要邮箱验证")
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
    cookie: str, random_reward: bool, *, proxy_url: str | None = None,
    browser_service: str | None = None, credentials: dict | None = None,
    save_session=None, email_code=None, pending_login=None, save_pending=None, verify_only=False,
) -> AttendanceResult:
    """Reuse the session; refresh login once only after an explicit auth rejection."""
    if browser_service or credentials or email_code:
        return _managed_attendance(cookie, random_reward, proxy_url=proxy_url,
                                   browser_service=browser_service, credentials=credentials,
                                   save_session=save_session, email_code=email_code,
                                   pending_login=pending_login, save_pending=save_pending, verify_only=verify_only)
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


def _managed_attendance(cookie, random_reward, *, proxy_url, browser_service, credentials, save_session,
                        email_code=None, pending_login=None, save_pending=None, verify_only=False):
    from .browser import BrowserServiceError, create_browser_session

    started = perf_counter()
    meta = {"stage": "签到"}
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT,
               "Origin": FORUM_URL.rstrip("/"), "Referer": FORUM_URL + "board"}
    jar = {}
    session_factory = requests.Session
    session_options = {}
    transport_errors = (requests.RequestException,)

    def finish(result):
        return replace(result, **meta, elapsed_ms=round((perf_counter() - started) * 1000))

    if browser_service:
        try:
            from curl_cffi import requests as browser_requests
        except ImportError:
            return finish(AttendanceResult("service_error", "缺少浏览器请求组件，本次未提交签到。", reason="curl_cffi 未安装"))
        try:
            browser = create_browser_session(browser_service, proxy_url)
        except BrowserServiceError as error:
            return replace(error.result, elapsed_ms=round((perf_counter() - started) * 1000))
        meta.update(service_http=200, service_ms=browser.elapsed_ms)
        headers.pop("User-Agent")
        headers.update(browser.headers)
        jar.update(browser.cookies)
        session_factory = browser_requests.Session
        session_options = {"impersonate": "chrome"}
        transport_errors += (browser_requests.exceptions.RequestException,)
    if cookie:
        jar.update(dict(item.strip().split("=", 1) for item in normalize_cookie(cookie).split(";")))

    try:
        with session_factory(**session_options) as session:
            session.trust_env = False
            for name, value in jar.items():
                session.cookies.set(name, value, domain="www.nodeseek.com", path="/")

            def request(path, *, method="POST", **kwargs):
                response = session.request(method,
                    FORUM_URL + path, headers=headers, timeout=(6, 25),
                    proxies={"http": proxy_url, "https": proxy_url} if proxy_url else {},
                    allow_redirects=False, stream=True, **kwargs)
                try:
                    meta["http_status"] = response.status_code
                    meta["response_type"] = _response_type(response.headers)
                    blocked = _blocked(response.status_code, response.headers)
                    if blocked and not (response.status_code in {400, 401, 403, 500} and meta["response_type"] == "JSON"
                                        and result_is_not_challenge(response.headers)):
                        return blocked, None
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=8192):
                        if len(body) + len(chunk) > MAX_RESPONSE:
                            return AttendanceResult("uncertain", "响应超出限制，结果待确认。", reason="响应体超出限制"), None
                        body.extend(chunk)
                    try:
                        payload = json.loads(body)
                    except (ValueError, UnicodeError):
                        payload = None
                    result = interpret_result(body)
                    if blocked and result.status not in {"already", "session_expired", "session_required", "email_required", "verification"}:
                        result = blocked
                    return result, payload
                finally:
                    response.close()

            def save_authenticated_session():
                fresh = {}
                for entry in session.cookies.jar:
                    if entry.domain.lstrip(".") in {"www.nodeseek.com", "nodeseek.com"} and entry.name in SESSION_KEYS:
                        fresh[entry.name] = entry.value
                try:
                    new_cookie = normalize_cookie("; ".join(f"{k}={v}" for k, v in fresh.items()))
                except ValueError:
                    return AttendanceResult("login_failed", "登录未返回完整会话，请在浏览器完成验证。", reason="登录缺少会话 Cookie")
                if not save_session:
                    return AttendanceResult("config_error", "无法保存新会话，本次未提交签到。", reason="会话持久化不可用")
                try:
                    save_session(new_cookie)
                    if save_pending:
                        save_pending({})
                except OSError:
                    return AttendanceResult("config_error", "新会话保存失败，本次未提交签到。", reason="会话写入失败")
                return None

            def request_email(email):
                if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                    return AttendanceResult("email_required", "请在插件设置填写绑定邮箱，再保存并立即执行以发送验证码。", reason="缺少绑定邮箱")
                pending = {"email": email, "created_at": time()}
                if save_pending:
                    save_pending(pending)
                if not browser_service:
                    return AttendanceResult("email_required", "请在 NodeSeek 邮箱登录页面获取验证码，再填入插件继续。", reason="等待邮件验证码")
                try:
                    token = create_browser_session(browser_service, proxy_url, mode="turnstile-min", page="emailSignIn.html")
                except BrowserServiceError:
                    return AttendanceResult("email_required", "邮件发送前的验证未完成，请在 NodeSeek 邮箱登录页面获取验证码，再填入插件。", reason="邮件发送验证未完成")
                _, response = request("api/email", json={"email": email, "mode": "totp", "token": token,
                                                            "source": "turnstile", "version": "v3"})
                if isinstance(response, dict) and response.get("success") is True:
                    return AttendanceResult("email_required", "验证码邮件已请求发送，请在插件设置填写邮件验证码并保存，随后自动继续签到。", reason="邮件已发送，等待输入")
                return AttendanceResult("email_required", "邮件发送未成功，请在 NodeSeek 邮箱登录页面获取验证码，再填入插件。", reason="发送验证码失败")

            def login():
                nonlocal login_used
                login_used = True
                meta["stage"] = "账号登录"
                if not browser_service:
                    return AttendanceResult("login_failed", "自动账号登录需要配置浏览器验证服务。", reason="未配置登录验证服务")
                try:
                    token = create_browser_session(browser_service, proxy_url, mode="turnstile-min", page="signIn.html")
                except BrowserServiceError as error:
                    meta["service_http"] = error.result.service_http
                    meta["service_ms"] = (meta.get("service_ms") or 0) + (error.result.service_ms or 0)
                    return error.result
                headers["x-captcha-token"] = token
                headers["x-captcha-source"] = "turnstile"
                result, payload = request("api/account/signIn", json={
                    "username": credentials["username"], "password": credentials["password"]})
                headers.pop("x-captcha-token", None)
                headers.pop("x-captcha-source", None)
                meta["login_http"] = meta.get("http_status")
                if isinstance(payload, dict):
                    redirect = payload.get("redirect")
                    if isinstance(redirect, str) and "emailsignin" in urlsplit(redirect).path.lower():
                        email = parse_qs(urlsplit(redirect).query).get("email", [None])[0] or credentials.get("email") or credentials.get("username")
                        return request_email(email)
                    if payload.get("need2FA"):
                        return AttendanceResult("login_failed", "账号启用了额外的 2FA 验证，请在浏览器登录后更新 Cookie。", reason="需要账号二次验证")
                    if payload.get("success") is True and not redirect:
                        pass
                    elif payload.get("success") is True and isinstance(redirect, str) and urlsplit(redirect).path in {"/", "/board", ""}:
                        pass
                    else:
                        if result.status in {"verification", "email_required", "rate_limited"}:
                            return result
                        return AttendanceResult("login_failed", "自动登录未成功，请检查账号密码或在浏览器完成登录。", reason="登录接口未确认完成")
                else:
                    return result
                return save_authenticated_session()

            login_used = False
            if email_code:
                login_used = True
                meta["stage"] = "邮件验证"
                email = (pending_login or {}).get("email") or (credentials or {}).get("email") or (credentials or {}).get("username")
                if not email or not re.fullmatch(r"[A-Za-z0-9]{4,12}", email_code):
                    return finish(AttendanceResult("email_required", "请检查绑定邮箱和邮件验证码格式。", reason="验证码输入无效"))
                for entry in list(session.cookies.jar):
                    if entry.name in SESSION_KEYS:
                        session.cookies.jar.clear(entry.domain, entry.path, entry.name)
                result, payload = request("api/account/emailSignIn", json={"email": email, "code": email_code})
                meta["login_http"] = meta.get("http_status")
                if not isinstance(payload, dict) or payload.get("success") is not True:
                    return finish(AttendanceResult("email_required", "邮件验证码未通过，请检查验证码或重新获取后填写。", reason="邮件验证未完成"))
                failure = save_authenticated_session()
                if failure:
                    return finish(failure)
            elif pending_login and pending_login.get("email"):
                return finish(request_email(pending_login["email"]))
            elif not cookie:
                if not credentials:
                    return finish(AttendanceResult("cookie_missing", "请配置登录 Cookie 或账号密码。"))
                result = login()
                if result:
                    return finish(result)
            def check_today():
                meta["stage"] = "签到状态检查"
                result, payload = request("api/attendance/board", method="GET", params={"page": 1})
                if meta.get("http_status") == 200 and isinstance(payload, dict) and "record" in payload:
                    record = payload.get("record")
                    if isinstance(record, dict):
                        try:
                            stamp = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
                            zone = timezone(timedelta(hours=8))
                            if stamp.tzinfo and stamp.astimezone(zone).date() == datetime.now(zone).date():
                                gain = record.get("gain")
                                return AttendanceResult("already", "站点记录确认今天已签到，无需重复提交。",
                                    gain if type(gain) is int and gain >= 0 else None, reason="已核对今日签到记录")
                        except (ValueError, KeyError, TypeError):
                            return AttendanceResult("uncertain", "站点签到记录格式异常，请核对今日结果。", reason="无法核对签到日期")
                    return None
                return result if result.status != "failed" else AttendanceResult("failed", "无法查询今日签到状态，本次未提交签到。", reason="签到状态查询失败")

            result = check_today()
            if result and cookie and credentials and not login_used and result.status in {"session_expired", "session_required"}:
                # Clear rejected account cookies before login, keeping the fresh browser session.
                for entry in list(session.cookies.jar):
                    if entry.name in SESSION_KEYS:
                        session.cookies.jar.clear(entry.domain, entry.path, entry.name)
                failure = login()
                if failure:
                    return finish(failure)
                result = check_today()
            if result:
                return finish(result)
            if verify_only:
                return finish(AttendanceResult("uncertain", "尚未查到今日签到记录，未重复提交结果不明的请求。", reason="仅核对记录"))
            meta["stage"] = "签到"
            result, _ = request("api/attendance", params={"random": str(random_reward).lower()})
            if result.status in {"session_expired", "session_required"} and credentials and not login_used:
                for entry in list(session.cookies.jar):
                    if entry.name in SESSION_KEYS:
                        session.cookies.jar.clear(entry.domain, entry.path, entry.name)
                failure = login()
                if failure:
                    return finish(failure)
                checked = check_today()
                if checked:
                    return finish(checked)
                meta["stage"] = "签到（自动登录后）"
                result, _ = request("api/attendance", params={"random": str(random_reward).lower()})
            return finish(result)
    except transport_errors as error:
        # Do not retry ambiguous attendance failures: the first request may have succeeded.
        if meta["stage"] == "账号登录":
            return finish(AttendanceResult("login_failed", "登录连接失败，请检查网络后重试。", reason=type(error).__name__))
        return finish(AttendanceResult("uncertain", "签到连接中断，结果未知，请先核对今日签到。", reason=type(error).__name__))


def result_is_not_challenge(headers):
    return (headers.get("cf-mitigated") or "").casefold() != "challenge"
