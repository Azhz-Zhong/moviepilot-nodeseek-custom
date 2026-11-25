"""NodeSeek daily attendance plugin for MoviePilot V3."""

from datetime import datetime, timedelta, timezone
import os
import re
import tempfile
from threading import RLock

from apscheduler.triggers.cron import CronTrigger

from app.schemas.types import MessageType
from app.sdk import scheduler
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .client import AttendanceResult, FORUM_URL, normalize_cookie, submit_attendance


ZONE = timezone(timedelta(hours=8), "UTC+08:00")
TIME_FORMAT = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d", re.ASCII)
LOG_KEY = "attendance_log"
GATE_KEY = "session_block"
DONE = {"success", "already", "confirmed"}
PAUSED = {"session_required", "verification"}
ONCE_JOB = "attendance_once"
STATUS = {
    "success": "签到成功", "already": "已签到", "confirmed": "手动确认",
    "pending": "请求进行中", "uncertain": "结果待确认", "failed": "失败",
    "verification": "需要浏览器验证", "session_required": "需要更新 Cookie",
    "rate_limited": "请求受限", "skipped": "跳过", "configured": "已保存会话", "config_error": "配置失败",
    "proxy_required": "代理未配置", "proxy_error": "代理连接失败", "connect_error": "网络连接超时",
}
DEFAULTS = {
    "auto_enabled": False, "attendance_time": "08:30", "random_reward": False,
    "use_proxy": True, "notify": False,
    "session_cookie": "", "run_once": False,
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
    plugin_desc = "Cookie 自动签到、收益图表和执行日志。"
    plugin_icon = "https://raw.githubusercontent.com/Azhz-Zhong/moviepilot-nodeseek-custom/main/icons/nodeseek.png"
    plugin_version = "0.3.1"
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

    def init_plugin(self, config=None):
        """Apply settings and consume write-only inputs and one-time actions."""
        config = config or {}
        clean = dict(config)
        raw_cookie = clean.pop("session_cookie", None)
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
            if raw_cookie:
                self._configure_session(raw_cookie)
            if run_once and not scheduler.add_plugin_once_job(
                self.__class__.__name__, ONCE_JOB, self._manual_run, "NS 立即签到", delay_seconds=3
            ):
                self._append(AttendanceResult("failed", "签到调度尚未就绪，请稍后再试。"), "手动执行")

    def get_state(self):
        """Report whether the daily schedule can submit a request."""
        with self._lock:
            return (
                self._active and self._enabled and TIME_FORMAT.fullmatch(self._time) is not None
                and self._cookie_path().is_file() and not self.get_data(GATE_KEY)
            )

    def get_api(self):
        """Use the host's authenticated settings and detail endpoints."""
        return []

    def get_form(self):
        """Offer write-only Cookie input, scheduling, and a manual run control."""
        return [
            component("VAlert", type="info", variant="tonal", **{"class": "mb-3"},
                      text="可直接粘贴浏览器复制的整段 Cookie，插件只保存 session 和 pjwt。保存后输入框会清空。"),
            component("VTextField", model="session_cookie", type="text", label="登录后的 Cookie",
                      placeholder="粘贴完整 Cookie 或 session=…; pjwt=…", autocomplete="off", spellcheck=False),
            component("VSwitch", model="auto_enabled", label="启用每日自动签到"),
            component("VTextField", model="attendance_time", type="time", label="签到时间（北京时间）"),
            component("VSwitch", model="random_reward", label="随机奖励"),
            component("VSwitch", model="use_proxy", label="使用 MoviePilot 系统代理"),
            component("VSwitch", model="notify", label="推送签到结果通知"),
            component("VSwitch", model="run_once", label="保存并立即签到一次"),
        ], {**DEFAULTS, "attendance_time": self._time}

    def get_page(self):
        """Render the session status, seven-day gain chart, and recent executions."""
        with self._lock:
            records = self._records()
            enabled = self._enabled
            use_proxy = self._use_proxy
            time = self._time
            exists = self._cookie_path().is_file()
            gate = self.get_data(GATE_KEY)
        if not exists:
            state = ("info", "未配置 Cookie", "点击插件设置，填写浏览器登录后的会话 Cookie。")
        elif gate in PAUSED:
            state = ("warning", STATUS[gate], "请在 NodeSeek 浏览器页面完成登录或验证，之后更新 Cookie。")
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
            }
            for row in records
        ]
        return [
            component("VAlert", type=tone, title=title, variant="tonal", **{"class": "mb-3"}, text=detail),
            component("VBtn", text="打开 NodeSeek", href=FORUM_URL, target="_blank",
                      rel="noopener noreferrer", variant="tonal", **{"class": "mb-3"}),
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
                ], items=rows, density="compact", **{"items-per-page": 10, "no-data-text": "尚无执行日志。"}),
            ]),
        ]

    def get_service(self):
        """Register the daily job once a normal session is configured."""
        with self._lock:
            if not self.get_state():
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
            if self._has_status_today(records, DONE | {"pending", "uncertain", "rate_limited"}):
                if source != "每日计划":
                    self._append(AttendanceResult("skipped", "今天已签到或已有结果待确认，请查看已有日志。"), source)
                return
            if self.get_data(GATE_KEY):
                if source != "每日计划":
                    self._append(AttendanceResult("skipped", "更新 Cookie 后可继续执行。"), source)
                return
            try:
                cookie = normalize_cookie(self._cookie_path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._record_and_notify(AttendanceResult("session_required", "请先配置有效的登录 Cookie。"), source)
                return
            proxy_url = self._proxy_url() if self._use_proxy else None
            if self._use_proxy and proxy_url is None:
                self._record_and_notify(AttendanceResult("proxy_required", "请在 MoviePilot 的网络设置中配置系统代理。"), source)
                return
            # Leave a pending record across process restarts before submitting the request.
            pending = self._row(AttendanceResult("pending", "签到请求已开始。"), source)
            self._save_log([pending, *self._stored_log()])
            result = submit_attendance(cookie, self._random_reward, proxy_url=proxy_url)
            if result.status in PAUSED:
                self.save_data(GATE_KEY, result.status)
            stored = self._stored_log()
            finished = {**pending, "status": result.status, "message": result.message, "gain": result.gain}
            self._save_log([finished, *stored[1:]])
            logger.info(f"NS每日签到执行结果：{result.status}")
            self._notify_result(result, source)

    @staticmethod
    def _proxy_url():
        return (settings.PROXY or {}).get("https") or None

    def _record_and_notify(self, result, source):
        self._append(result, source)
        self._notify_result(result, source)

    def _notify_result(self, result, source):
        if not self._notify:
            return
        gain = f"\n获得 {result.gain} 个鸡腿。" if result.gain is not None else ""
        try:
            self.post_message(
                mtype=MessageType.SiteMessage,
                title=f"NS每日签到 · {STATUS[result.status]}",
                text=f"{source}。{result.message}{gain}",
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
        return any(row["day"] == today and row["status"] in statuses for row in records)

    def _row(self, result, source):
        now = self._now()
        return {"day": now.date().isoformat(), "time": now.isoformat(timespec="seconds"),
                "status": result.status, "message": result.message, "gain": result.gain, "source": source}

    def _chart(self, records):
        today = self._now().date()
        days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        gains = {}
        confirmed = set()
        for row in records:
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
