"""合成资金接口适配。

对接 partner/server.py 的回放协议：
- POST /commands 逐作业项受理；202 已收到，504 响应不明但协作方可能已受理。
- GET /commands/<id> 轮询原始流水事实（pending/paid/rejected）。
- 断线（504）的作业项仍占用原付款额度，只有明确 rejected 才能重新办理。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .errors import AppError, bad_gateway, conflict


def _request(base: str, method: str, path: str, payload: dict | None, timeout: float):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except (ValueError, UnicodeDecodeError):
            body = {}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise bad_gateway(f"资金协作方不可达: {exc}")


class FundAdapter:
    def __init__(self, base_url: str | None, timeout: float = 5.0):
        self.base_url = base_url
        self.timeout = timeout

    def send(self, command_id: str, operation: str, request_payload: dict) -> dict:
        if not self.base_url:
            raise bad_gateway("未配置资金协作方地址 FUND_BASE_URL")
        status, body = _request(
            self.base_url, "POST", "/commands",
            {"command_id": command_id, "operation": operation, "request": request_payload},
            self.timeout,
        )
        # 202 与 504 都意味着协作方已受理或可能已受理；400/409 为协议层拒绝
        if status not in (202, 504):
            raise conflict(
                "fund_rejected_dispatch",
                f"资金协作方拒绝受理流水 {command_id}: {body.get('error', status)}",
            )
        return {"http_status": status, "body": body}

    def fetch(self, command_id: str) -> dict:
        if not self.base_url:
            raise bad_gateway("未配置资金协作方地址 FUND_BASE_URL")
        status, body = _request(self.base_url, "GET", f"/commands/{command_id}", None, self.timeout)
        if status == 404:
            raise AppError(409, "fund_not_accepted", f"流水 {command_id} 尚未被协作方受理")
        if status >= 400:
            raise bad_gateway(f"资金协作方查询失败: {body.get('error', status)}")
        return body
