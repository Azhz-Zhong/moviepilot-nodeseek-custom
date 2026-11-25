"""MoviePilot V3 helpers for user-confirmed NodeSeek attendance."""

from datetime import datetime, timedelta, timezone
from threading import RLock
import re

from apscheduler.triggers.cron import CronTrigger

from app.schemas.types import MessageType
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase


FORUM_URL = "https://www.nodeseek.com/"
FORUM_TIMEZONE = timezone(timedelta(hours=8), "UTC+08:00")
DEFAULT_TIME = "08:30"
HISTORY_LIMIT = 30
TIME_FORMAT = re.compile(r"([01]\d|2[0-3]):([0-5]\d)", re.ASCII)
HISTORY_KEY = "confirmed_attendance"
REMINDER_KEY = "last_reminder_date"


class NodeSeekSignCustom(_PluginBase):
    """Open the forum, remind the user, and retain attendance confirmations."""

    plugin_name = "NodeSeek 签到助手"
    plugin_desc = "提供浏览器签到入口、每日提醒和确认记录。"
    plugin_icon = ""
    plugin_version = "0.1.0"
    plugin_author = "Azhz-Zhong"
    author_url = "https://github.com/Azhz-Zhong"
    plugin_config_prefix = "nodeseeksigncustom_"
    plugin_order = 30
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._lock = RLock()
        self._remind_enabled = False
        self._reminder_time = DEFAULT_TIME

    def init_plugin(self, config=None):
        """Apply settings and consume an explicit confirmation from the form."""
        config = config or {}
        with self._lock:
            self._remind_enabled = config.get("remind_enabled") is True
            value = config.get("reminder_time", DEFAULT_TIME)
            self._reminder_time = value.strip() if isinstance(value, str) else ""
            if config.get("confirm_today") is True:
                self._confirm_attendance()
                self.update_config({**config, "confirm_today": False})

    def get_state(self):
        """Show whether the daily reminder is enabled and configured."""
        with self._lock:
            return self._remind_enabled and TIME_FORMAT.fullmatch(self._reminder_time) is not None

    def get_api(self):
        """All controls are provided by the host's standard plugin forms."""
        return []

    def get_form(self):
        """Render daily reminder settings and the one-time confirmation control."""
        fields = [
            {"component": "VSwitch", "props": {"model": "remind_enabled", "label": "启用每日提醒"}},
            {
                "component": "VTextField",
                "props": {
                    "model": "reminder_time",
                    "type": "time",
                    "label": "提醒时间（北京时间）",
                },
            },
            {
                "component": "VSwitch",
                "props": {
                    "model": "confirm_today",
                    "label": "我已在论坛完成今天的签到",
                    "hint": "勾选并保存后记入今天的签到记录，同一天只记一次。",
                    "persistent-hint": True,
                },
            },
        ]
        return fields, {"remind_enabled": False, "reminder_time": DEFAULT_TIME, "confirm_today": False}

    def get_page(self):
        """Render the forum shortcut and the latest user-confirmed attendance."""
        with self._lock:
            history = self._history()
            today = self._now().date().isoformat()
            confirmed = any(record["date"] == today for record in history)
            invalid_time = self._remind_enabled and not self.get_state()

        page = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if confirmed else "info",
                    "title": "今天已确认签到" if confirmed else "今天待确认",
                    "text": "在 NodeSeek 完成签到后，打开插件配置，勾选「我已在论坛完成今天的签到」并保存。",
                    "variant": "tonal",
                    "class": "mb-4",
                },
            },
            {
                "component": "VBtn",
                "props": {
                    "href": FORUM_URL,
                    "target": "_blank",
                    "rel": "noopener noreferrer",
                    "color": "primary",
                    "class": "mb-4",
                },
                "text": "打开 NodeSeek",
            },
        ]
        if invalid_time:
            page.append({
                "component": "VAlert",
                "props": {"type": "warning", "text": "请按 HH:mm 格式填写有效的提醒时间。"},
            })
        page.append({
            "component": "VList",
            "props": {"density": "compact"},
            "content": [
                {
                    "component": "VListItem",
                    "props": {
                        "title": record["date"],
                        "subtitle": "确认于 " + record["confirmed_at"][11:16] + "（北京时间）",
                    },
                }
                for record in history
            ] or [{"component": "VListItem", "props": {"title": "暂无签到记录"}}],
        })
        return page

    def get_service(self):
        """Register one daily reminder with the MoviePilot scheduler."""
        with self._lock:
            if not self.get_state():
                return []
            hour, minute = self._reminder_time.split(":")
        return [{
            "id": "NodeSeekSignCustom.Reminder",
            "name": "NodeSeek 签到提醒",
            "trigger": CronTrigger(hour=int(hour), minute=int(minute), timezone=FORUM_TIMEZONE),
            "func": self.send_reminder,
            "kwargs": {},
        }]

    def stop_service(self):
        """Mark the instance stopped so an old callback can exit after reload."""
        with self._lock:
            self._remind_enabled = False

    def send_reminder(self):
        """Send at most one reminder per day while attendance is unconfirmed."""
        with self._lock:
            if not self.get_state():
                return
            today = self._now().date().isoformat()
            if self.get_data(REMINDER_KEY) == today or any(row["date"] == today for row in self._history()):
                return
            self.post_message(
                mtype=MessageType.Plugin,
                title="NodeSeek 签到提醒",
                text="今天的签到还未确认。请在浏览器完成签到，再回到 MoviePilot 保存确认。",
                link=FORUM_URL,
            )
            self.save_data(REMINDER_KEY, today)
            logger.info("NodeSeek 签到助手已发送今日提醒")

    def _confirm_attendance(self):
        now = self._now()
        today = now.date().isoformat()
        history = self._history()
        if not any(row["date"] == today for row in history):
            history.insert(0, {"date": today, "confirmed_at": now.isoformat(timespec="seconds")})
            self.save_data(HISTORY_KEY, history[:HISTORY_LIMIT])

    def _history(self):
        records = self.get_data(HISTORY_KEY)
        return records if isinstance(records, list) else []

    @staticmethod
    def _now():
        return datetime.now(FORUM_TIMEZONE)
