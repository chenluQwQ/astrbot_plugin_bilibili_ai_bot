"""总结存档：``daily_summary.json`` 和 ``weekly_summary.json`` 只由这里写，也由这里判断今天/本周做过没有。"""
from __future__ import annotations

from datetime import datetime

from ..infra.host_view import HostView


class SummaryArchive(HostView):
    HOST_API = frozenset({"states", "json_store"})

    def _daily_summary_done_today(self):
        records = self.states.daily_summary.read([])
        today = datetime.now().strftime("%Y-%m-%d")
        return any(isinstance(item, dict) and item.get("date") == today for item in (records if isinstance(records, list) else []))

    def _save_daily_summary_record(self, summary, delivered, image_path="", structured=None):
        records = self.states.daily_summary.read([])
        if not isinstance(records, list):
            records = []
        records.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
            "delivered": delivered,
            "image_path": image_path or "",
            "structured": structured if isinstance(structured, dict) else {},
        })
        self.states.daily_summary.write(records[-45:])

    def _weekly_summary_done_this_week(self):
        """检查本ISO周是否已生成过周总结。"""
        records = self.states.weekly_summary.read([])
        if not records:
            return False
        this_week = datetime.now().strftime("%G-W%V")
        return any(r.get("week") == this_week for r in records if isinstance(r, dict))

    def _save_weekly_summary_record(self, summary, delivered, image_path="", structured=None):
        records = self.states.weekly_summary.read([])
        records.append({
            "week": datetime.now().strftime("%G-W%V"),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": summary,
            "delivered": delivered,
            "image_path": image_path or "",
            "structured": structured if isinstance(structured, dict) else {},
        })
        self.states.weekly_summary.write(records[-20:])
