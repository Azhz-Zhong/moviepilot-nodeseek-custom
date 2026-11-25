"""NodeSeek daily attendance plugin for MoviePilot V3."""

from datetime import datetime, timedelta, timezone
import os
import json
import re
import tempfile
from threading import RLock
from uuid import uuid4

from apscheduler.triggers.cron import CronTrigger

from app.schemas.types import MessageType
from app.sdk import scheduler
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .client import AttendanceResult, FORUM_URL, normalize_cookie, submit_attendance
from .browser import normalize_service_url


ZONE = timezone(timedelta(hours=8), "UTC+08:00")
TIME_FORMAT = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d", re.ASCII)
LOG_KEY = "attendance_log"
GATE_KEY = "session_block"
DONE = {"success", "already", "confirmed"}
PAUSE_GUIDANCE = {
    "email_required": "请获取 NodeSeek 邮件验证码，在插件设置中填写后保存，插件会继续登录及签到。",
    "login_failed": "请检查账号密码，保存设置并立即执行可重试登录。",
    "session_expired": "站点报告会话已过期，请在浏览器登录后更新 Cookie。",
    "session_required": "站点要求登录。若浏览器仍已登录，可检查执行日志里的 HTTP 状态再更新 Cookie。",
    "verification": "站点要求浏览器验证，自动签到已暂停。",
    "forbidden": "站点拒绝了当前请求，请查看执行日志中的 HTTP 状态和原因。",
}
ONCE_JOB = "attendance_once"
STATUS = {
    "success": "签到成功", "already": "已签到", "confirmed": "手动确认",
    "pending": "请求进行中", "uncertain": "结果待确认", "failed": "失败",
    "verification": "需要浏览器验证", "session_required": "需要登录",
    "session_expired": "Cookie 已过期", "cookie_missing": "未配置有效 Cookie", "forbidden": "站点拒绝访问",
    "rate_limited": "请求受限", "skipped": "跳过", "configured": "已保存会话", "config_error": "配置失败",
    "proxy_required": "代理未配置", "proxy_error": "代理连接失败", "connect_error": "网络连接超时",
    "email_required": "等待邮件验证码", "login_failed": "登录未完成", "service_error": "验证服务失败",
}
DEFAULTS = {
    "auto_enabled": False, "attendance_time": "08:30", "random_reward": False,
    "use_proxy": True, "notify": False,
    "session_cookie": "", "run_once": False,
    "browser_service_url": "", "account_username": "", "account_password": "", "email_code": "",
}


def component(kind, *, text=None, content=None, **props):
    """Build a MoviePilot Vuetify element."""
    element = {"component": kind}
    if props:
        element["props"] = props
    if text is not None:
        element["text"] = text
    if content is not None:
        element["content"] = content
    return element


class NodeSeekSignCustom(_PluginBase):
    """Schedule attendance, store the account session, and display local results."""

    plugin_name = "NS每日签到"
    plugin_desc = "自动登录、每日签到、邮件验证续接与收益日志。"
    plugin_icon = "https://raw.githubusercontent.com/Azhz-Zhong/moviepilot-nodeseek-custom/main/icons/nodeseek.png"
    plugin_version = "0.4.1"
    plugin_author = "Azhz-Zhong"
    author_url = "https://github.com/Azhz-Zhong"
    plugin_config_prefix = "nodeseeksigncustom_"
    plugin_order = 30
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._lock = RLock()
        self._active = False
        self._enabled = False
        self._time = DEFAULTS["attendance_time"]
        self._random_reward = False
        self._use_proxy = DEFAULTS["use_proxy"]
        self._notify = DEFAULTS["notify"]
        self._email_code = ""

    def init_plugin(self, config=None):
        """Apply settings and consume write-only inputs and one-time actions."""
        config = config or {}
        clean = dict(config)
        raw_cookie = clean.pop("session_cookie", None)
        raw_service = clean.pop("browser_service_url", None)
        username = clean.pop("account_username", None)
        password = clean.pop("account_password", None)
        email = clean.pop("account_email", None)
        email_code = clean.pop("email_code", None)
        run_once = clean.pop("run_once", False) is True
        # Drop saved switches from earlier releases without replaying their actions.
        clean.pop("clear_cookie", None)
        clean.pop("confirm_today", None)
        with self._lock:
            self._active = True
            self._enabled = clean.get("auto_enabled") is True
            value = clean.get("attendance_time", clean.get("reminder_time", DEFAULTS["attendance_time"]))
            self._time = value.strip() if isinstance(value, str) else ""
            self._random_reward = clean.get("random_reward") is True
            self._use_proxy = clean.get("use_proxy", DEFAULTS["use_proxy"]) is True
            self._notify = clean.get("notify") is True
            if clean != config:
                self.update_config(clean)
            if raw_service:
                try:
                    self._write_private("browser-service.json", json.dumps({"url": normalize_service_url(raw_service)}))
                    if self.get_data(GATE_KEY) in {"verification", "forbidden"}:
                        self.save_data(GATE_KEY, "")
                except (ValueError, OSError):
                    self._append(AttendanceResult("config_error", "验证服务配置保存失败，请检查地址格式。"), "设置")
            account = self._private_json("account.json")
            previous_email = str(account.get("username") or account.get("email") or "").strip().casefold()
            reset_session = False
            if username or password or email or account.get("email"):
                # One email identifies both password login and email-code login.
                canonical_email = str(username or email or account.get("email") or account.get("username") or "").strip()
                account["username"] = canonical_email
                account.pop("email", None)
                if password:
                    account["password"] = str(password)
                changed = bool(previous_email and previous_email != canonical_email.casefold())
                if changed and not password:
                    self._append(AttendanceResult("config_error", "更换登录邮箱时请同时填写该账号的密码，原账号保持不变。"), "设置")
                    account = self._private_json("account.json")
                elif re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", canonical_email) and account.get("password"):
                    try:
                        self._write_private("account.json", json.dumps(account))
                        reset_session = changed
                        if self.get_data(GATE_KEY) in {"login_failed", "session_required", "session_expired"}:
                            self.save_data(GATE_KEY, "")
                    except OSError:
                        self._append(AttendanceResult("config_error", "账号配置保存失败。"), "设置")
                else:
                    self._append(AttendanceResult("config_error", "请填写有效的登录邮箱；首次配置需同时填写密码。"), "设置")
            account = self._private_json("account.json")
            pending = self._private_json("pending-login.json")
            mismatch = bool(pending.get("email") and account.get("username") and
                            pending["email"].casefold() != account["username"].casefold())
            if reset_session or mismatch:
                self._write_private("session.cookie", "")
                self._write_private("pending-login.json", "{}")
                self.save_data(GATE_KEY, "")
                self.save_data("account_scope", uuid4().hex)
                self._email_code = ""
                if email_code:
                    email_code = None
                    run_once = False
                self._append(AttendanceResult("configured", "已按当前登录邮箱重置旧会话与验证码状态，请重新登录。"), "设置")
            if raw_cookie:
                self._configure_session(raw_cookie)
            if email_code:
                self._email_code = str(email_code).strip()
                run_once = True
            if self._private_json("pending-login.json").get("email") and not self.get_data(GATE_KEY):
                self.save_data(GATE_KEY, "email_required")
            if run_once and self.get_data(GATE_KEY) == "login_failed":
                self.save_data(GATE_KEY, "")
            if run_once and not scheduler.add_plugin_once_job(
                self.__class__.__name__, ONCE_JOB, self._manual_run, "NS 立即签到", delay_seconds=3
            ):
                self._append(AttendanceResult("failed", "签到调度尚未就绪，请稍后再试。"), "手动执行")

    def get_state(self):
        """Keep the schedule registered while waiting for user verification."""
        with self._lock:
            return (
                self._active and self._enabled and TIME_FORMAT.fullmatch(self._time) is not None
                and bool(self._cookie_path().is_file() or self._private_json("account.json"))
            )

    def get_api(self):
        """Use the host's authenticated settings and detail endpoints."""
        return []

    def get_form(self):
        """Compact responsive login/schedule sections with optional advanced fields."""
        field = {"variant": "outlined", "density": "comfortable", "hide-details": "auto"}
        toggle = {"color": "primary", "density": "compact", "hide-details": True}
        account = self._private_json("account.json")
        account_exists = bool(account)
        service_exists = bool(self._private_json("browser-service.json").get("url"))

        def col(element, sm=6):
            return component("VCol", cols=12, sm=sm, content=[element])

        def section(title, children):
            return component("VCard", variant="outlined", rounded="lg", **{"class": "mb-3"}, content=[
                component("VCardTitle", text=title, **{"class": "text-subtitle-1 font-weight-medium pb-2"}),
                component("VCardText", content=[component("VRow", dense=True, content=children)]),
            ])

        return [
            component("VAlert", type="info", variant="tonal", density="compact", **{"class": "mb-3"},
                      text="使用下方登录邮箱自动登录；需要邮件验证码时通知你。密码留空保留，更换邮箱时请同时填写新密码。"),
            section("账号登录", [
                col(component("VTextField", model="account_username", type="email", label="登录邮箱", autocomplete="off",
                    hint="当前使用的邮箱，可修改" if account_exists else "同时用于账号登录与邮件验证", **field)),
                col(component("VTextField", model="account_password", type="password", label="登录密码", autocomplete="new-password",
                    hint="已保存，可留空" if account_exists else "首次配置请填写", **field)),
                col(component("VTextField", model="email_code", label="邮件验证码", autocomplete="off",
                    hint="收到验证通知后填写，保存即继续登录", **field, **{"persistent-hint": True}), sm=12),
            ]),
            section("每日签到", [
                col(component("VSwitch", model="auto_enabled", label="启用自动签到", **toggle)),
                col(component("VTextField", model="attendance_time", type="time", label="签到时间（北京时间）", **field)),
                col(component("VSwitch", model="random_reward", label="随机奖励", **toggle)),
                col(component("VSwitch", model="notify", label="推送签到结果", **toggle)),
            ]),
            component("VExpansionPanels", variant="accordion", **{"class": "mb-3"}, content=[
                component("VExpansionPanel", elevation=0, content=[
                    component("VExpansionPanelTitle", text="高级设置 · 网络与 Cookie"),
                    component("VExpansionPanelText", content=[component("VRow", dense=True, content=[
                        col(component("VTextField", model="browser_service_url", label="浏览器验证服务地址",
                            hint="已配置，留空保留" if service_exists else "仅保存在 MoviePilot 本机", **field,
                            **{"persistent-hint": True}), sm=12),
                        col(component("VSwitch", model="use_proxy", label="使用 MoviePilot 系统代理", **toggle), sm=12),
                        col(component("VTextarea", model="session_cookie", label="Cookie（可选）", rows=2,
                            hint="可手动导入完整 Cookie；填写时明文显示，保存后清空", autocomplete="off", spellcheck=False,
                            **field, **{"persistent-hint": True}), sm=12),
                    ])]),
                ]),
            ]),
            component("VSwitch", model="run_once", label="保存后立即检查并签到", **toggle),
        ], {**DEFAULTS, "attendance_time": self._time, "account_username": account.get("username", "")}

    def get_page(self):
        """Render the session status, seven-day gain chart, and recent executions."""
        with self._lock:
            records = self._records()
            enabled = self._enabled
            use_proxy = self._use_proxy
            time = self._time
            exists = self._cookie_path().is_file()
            gate = self.get_data(GATE_KEY)
        if not exists and not self._private_json("account.json"):
            state = ("info", "未配置 Cookie", "点击插件设置，填写浏览器登录后的会话 Cookie。")
        elif gate in PAUSE_GUIDANCE:
            state = ("warning", STATUS[gate], PAUSE_GUIDANCE[gate])
        elif enabled and use_proxy and not self._proxy_url():
            state = ("warning", "系统代理未配置", "请在 MoviePilot 的网络设置中配置代理。")
        elif self._has_status_today(records, DONE):
            state = ("success", "今日已签到", f"每日计划 {time}（北京时间）" if enabled else "每日自动签到已关闭。")
        elif enabled and not TIME_FORMAT.fullmatch(time):
            state = ("warning", "签到时间无效", "请在设置中按 HH:mm 填写北京时间。")
        else:
            state = ("info", "会话已配置", f"每日计划 {time}（北京时间）" if enabled else "可在设置中立即执行或启用每日签到。")
        tone, title, detail = state
        rows = [
            {
                "time": row["time"][:19].replace("T", " "),
                "source": row["source"],
                "state": STATUS.get(row["status"], row["status"]),
                "gain": row["gain"] if row.get("gain") is not None else "—",
                "message": row["message"],
                "diagnostic": self._diagnostics(row),
            }
            for row in records
        ]
        return [
            component("VAlert", type=tone, title=title, variant="tonal", **{"class": "mb-3"}, text=detail),
            component("VCard", variant="outlined", **{"class": "mb-3"}, content=[
                component("VCardTitle", text="近 7 天签到收益"),
                component("VCardText", content=self._chart(records)),
            ]),
            component("VCard", variant="outlined", content=[
                component("VCardTitle", text="执行日志（北京时间）"),
                component("VDataTable", headers=[
                    {"title": "时间", "key": "time"},
                    {"title": "来源", "key": "source"},
                    {"title": "状态", "key": "state"},
                    {"title": "鸡腿", "key": "gain"},
                    {"title": "结果", "key": "message"},
                    {"title": "诊断详情", "key": "diagnostic"},
                ], items=rows, density="compact", **{"items-per-page": 10, "no-data-text": "尚无执行日志。"}),
            ]),
        ]

    def get_service(self):
        """Register the daily job once a normal session is configured."""
        with self._lock:
            if not (self._active and self._enabled and TIME_FORMAT.fullmatch(self._time)
                    and (self._cookie_path().is_file() or self._private_json("account.json"))):
                return []
            hour, minute = map(int, self._time.split(":"))
        return [{
            "id": "NodeSeekSignCustom.Attendance", "name": "NS每日签到",
            "trigger": CronTrigger(hour=hour, minute=minute, timezone=ZONE),
            "func": self.run_attendance, "kwargs": {},
        }]

    def stop_service(self):
        """Prevent calls from a previous instance after reload."""
        with self._lock:
            self._active = False
        scheduler.remove_plugin_once_job(self.__class__.__name__, ONCE_JOB)

    def _manual_run(self):
        self.run_attendance("手动执行")

    def run_attendance(self, source="每日计划"):
        """Run one request, record its outcome, and avoid duplicate submissions."""
        with self._lock:
            if not self._active or (source == "每日计划" and not self._enabled):
                return
            records = self._records()
            verify_only = self._has_status_today(records, {"pending", "uncertain"})
            resuming = source != "每日计划" and (self._email_code or self.get_data(GATE_KEY) == "email_required")
            if resuming and self._has_status_today(records, DONE):
                verify_only = True
            if (self._has_status_today(records, DONE | {"rate_limited"}) and not resuming) or (verify_only and source == "每日计划"):
                if source != "每日计划":
                    self._append(AttendanceResult("skipped", "今天已签到或已有结果待确认，请查看已有日志。"), source)
                return
            if self.get_data(GATE_KEY) and not (source != "每日计划" and self.get_data(GATE_KEY) == "email_required"):
                if source != "每日计划":
                    self._append(AttendanceResult("skipped", "更新 Cookie 后可继续执行。"), source)
                return
            try:
                cookie = normalize_cookie(self._cookie_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cookie = ""
            account = self._private_json("account.json")
            if not cookie and not account:
                self._record_and_notify(AttendanceResult("cookie_missing", "请配置有效 Cookie 或账号密码。"), source)
                return
            browser_service = self._private_json("browser-service.json").get("url")
            proxy_url = self._proxy_url() if self._use_proxy else None
            if self._use_proxy and proxy_url is None:
                self._record_and_notify(AttendanceResult("proxy_required", "请在 MoviePilot 的网络设置中配置系统代理。"), source)
                return
            # Leave a pending record across process restarts before submitting the request.
            pending = self._row(AttendanceResult("pending", "签到请求已开始。"), source)
            pending["route"] = ("浏览器验证服务 + " if browser_service else "") + ("系统代理" if proxy_url else "直连")
            self._save_log([pending, *self._stored_log()])
            code, self._email_code = self._email_code, ""
            try:
                result = submit_attendance(cookie, self._random_reward, proxy_url=proxy_url,
                    browser_service=browser_service, credentials=account or None,
                    save_session=lambda value: self._write_private("session.cookie", normalize_cookie(value)),
                    email_code=code, pending_login=self._private_json("pending-login.json"),
                    save_pending=lambda value: self._write_private("pending-login.json", json.dumps(value)), verify_only=verify_only)
            except Exception as error:
                result = AttendanceResult("uncertain", "执行异常，结果待确认，请检查日志后再重试。", reason=type(error).__name__)
            if result.status in PAUSE_GUIDANCE and not (browser_service and result.status in {"verification", "forbidden"}):
                self.save_data(GATE_KEY, result.status)
            elif result.status in DONE:
                self.save_data(GATE_KEY, "")
            stored = self._stored_log()
            finished = {
                **pending, "status": result.status, "message": result.message, "gain": result.gain,
                "http_status": result.http_status, "elapsed_ms": result.elapsed_ms,
                "response_type": result.response_type, "reason": result.reason,
                "stage": result.stage, "service_http": result.service_http, "service_ms": result.service_ms, "login_http": result.login_http,
            }
            self._save_log([finished, *stored[1:]])
            logger.info(f"NS每日签到 {source} | {STATUS[result.status]} | {self._diagnostics(finished)}")
            self._notify_result(result, source)

    @staticmethod
    def _diagnostics(row):
        parts = [row[key] for key in ("route", "stage", "response_type", "reason") if row.get(key)]
        if row.get("service_http") is not None:
            parts.append(f"验证服务 HTTP {row['service_http']}")
        if row.get("service_ms") is not None:
            parts.append(f"验证耗时 {row['service_ms']} ms")
        if row.get("login_http") is not None:
            parts.append(f"登录 HTTP {row['login_http']}")
        if row.get("http_status") is not None:
            parts.insert(1, f"HTTP {row['http_status']}")
        if row.get("elapsed_ms") is not None:
            parts.append(f"{row['elapsed_ms']} ms")
        return " · ".join(parts) or "—"

    @staticmethod
    def _proxy_url():
        return (settings.PROXY or {}).get("https") or None

    def _record_and_notify(self, result, source):
        self._append(result, source)
        self._notify_result(result, source)

    def _notify_result(self, result, source):
        if not self._notify and result.status != "email_required":
            return
        gain = f"\n获得 {result.gain} 个鸡腿。" if result.gain is not None else ""
        try:
            self.post_message(
                mtype=MessageType.SiteMessage,
                title=f"NS每日签到 · {STATUS[result.status]}",
                text=f"{source}。{result.message}{gain}\n{self._diagnostics(result.__dict__)}",
                link=FORUM_URL,
            )
        except Exception:
            logger.warning("NS每日签到结果已记录，发送通知失败。")

    def _configure_session(self, raw_cookie):
        try:
            cookie = normalize_cookie(raw_cookie if isinstance(raw_cookie, str) else "")
            path = self._cookie_path()
            descriptor, temporary = tempfile.mkstemp(prefix=".session-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(cookie)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._write_private("pending-login.json", "{}")
        except ValueError as error:
            self._append(AttendanceResult("config_error", str(error)), "设置")
            return
        except OSError:
            self._append(AttendanceResult("config_error", "无法写入插件会话文件。"), "设置")
            return
        self.save_data(GATE_KEY, "")
        self._append(AttendanceResult("configured", "已保存会话 Cookie。"), "设置")

    def _cookie_path(self):
        return self.get_data_path() / "session.cookie"

    def _private_json(self, filename):
        try:
            value = json.loads((self.get_data_path() / filename).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_private(self, filename, content):
        path = self.get_data_path() / filename
        descriptor, temporary = tempfile.mkstemp(prefix=".private-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _append(self, result, source):
        self._save_log([self._row(result, source), *self._stored_log()])

    def _save_log(self, records):
        self.save_data(LOG_KEY, records[:100])

    def _stored_log(self):
        stored = self.get_data(LOG_KEY)
        return stored if isinstance(stored, list) else []

    def _records(self):
        records = list(self._stored_log())
        legacy = self.get_data("confirmed_attendance") or []
        if isinstance(legacy, list):
            records.extend(
                {"day": row["date"], "time": row["confirmed_at"], "source": "手动确认",
                 "status": "confirmed", "message": "旧版浏览器签到确认。", "gain": None}
                for row in legacy if isinstance(row, dict) and "date" in row and "confirmed_at" in row
            )
        return sorted(records, key=lambda row: row["time"], reverse=True)

    def _has_status_today(self, records, statuses):
        today = self._now().date().isoformat()
        scope = self.get_data("account_scope")
        return any(row.get("account_scope") == scope and row["day"] == today and row["status"] in statuses for row in records)

    def _row(self, result, source):
        now = self._now()
        return {"day": now.date().isoformat(), "time": now.isoformat(timespec="seconds"),
                "status": result.status, "message": result.message, "gain": result.gain, "source": source,
                "account_scope": self.get_data("account_scope")}

    def _chart(self, records):
        today = self._now().date()
        days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        gains = {}
        confirmed = set()
        for row in records:
            if row.get("account_scope") != self.get_data("account_scope"):
                continue
            if row["status"] in DONE:
                confirmed.add(row["day"])
                if row.get("gain") is not None:
                    gains.setdefault(row["day"], row["gain"])
        maximum = max(1, *(gains.get(day.isoformat(), 0) for day in days))
        return [
            component("VRow", dense=True, align="center", content=[
                component("VCol", cols=3, sm=2, text=day.strftime("%m-%d")),
                component("VCol", cols=5, sm=7, content=[component(
                    "VProgressLinear", max=maximum, height=8, rounded=True, color="primary",
                    **{"model-value": gains.get(key, 0), "aria-label": f"{key} 鸡腿收益"}
                )]),
                component("VCol", cols=4, sm=3, text=(
                    f"{gains[key]} 鸡腿" if key in gains else "已签到" if key in confirmed else "—"
                )),
            ])
            for day in days for key in (day.isoformat(),)
        ]

    @staticmethod
    def _now():
        return datetime.now(ZONE)
