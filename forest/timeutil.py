"""时间工具：统一东八区、ISO 解析、本地可控时钟。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
OBSERVATION_DAYS = 30  # 合成合同约定：管护观察期 30 日；第二次抽查距栽植不少于 30 天


def now_real() -> datetime:
    return datetime.now(TZ)


def parse_dt(value) -> datetime:
    """解析 ISO 时间；朴素时间按东八区处理，统一返回带时区时间。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"时间格式无法解析: {value}") from exc
    else:
        raise ValueError(f"时间格式无法解析: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat()


def add_days(dt: datetime, days: int) -> datetime:
    return dt + timedelta(days=days)


class Clock:
    """业务时钟：优先请求头 X-Now，其次库内覆盖值，最后真实时间。"""

    def __init__(self, db, header_value: str | None = None):
        self.db = db
        self.header_value = header_value

    def now(self) -> datetime:
        if self.header_value:
            return parse_dt(self.header_value)
        row = self.db.execute("SELECT value FROM kv WHERE key='clock.now_iso'").fetchone()
        if row:
            return parse_dt(row[0])
        return now_real()
