"""接口 HTTP 探测器——周期探测所有启用接口的可用性与响应时间。

每 INTERFACE_PROBE_INTERVAL 秒,对所有 enabled=1 的接口并发发 HTTP 请求;
状态码匹配 expected_status 记为正常(up=1),同时记录响应耗时,结果 upsert 到
interface_probes(每接口一行,业务健康聚合只读最新值)。

所有 DB 会话都在执行器线程内创建/关闭,不跨越 await 取消边界。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from app.config import INTERFACE_PROBE_CONCURRENCY, INTERFACE_PROBE_INTERVAL
from app.database import SessionLocal
from app.models.service_interface import InterfaceProbe, ServiceInterface
from app.services.crypto import decrypt
from app.validators import validate_outbound_url

logger = logging.getLogger(__name__)

# 探测器事件循环引用:同步路由线程(路由是 def,跑在线程池里)通过
# run_coroutine_threadsafe 触发立即探测。仅 run_interface_probe_loop 运行时非 None。
_loop: asyncio.AbstractEventLoop | None = None

# 连续探测失败计数(与 monitor 的离线消抖同语义):单次超时/网络抖动不立即
# 翻 down,连续 2 次才翻,避免业务健康闪烁。恢复 up 单次即生效。
_fail_streaks: dict[int, int] = {}
PROBE_CONFIRM_SCANS = 2


def _fetch_interfaces(interface_ids: list[int] | None = None) -> list[dict]:
    """读取启用接口配置(可选按 id 过滤)；敏感认证值仅在探测器内部解密。"""
    db = SessionLocal()
    try:
        query = db.query(ServiceInterface).filter(ServiceInterface.enabled == 1)
        if interface_ids is not None:
            query = query.filter(ServiceInterface.id.in_(interface_ids))
        rows = query.all()
        out = []
        for row in rows:
            try:
                headers = json.loads(row.request_headers_json or "{}")
                headers = headers if isinstance(headers, dict) else {}
            except (TypeError, ValueError):
                headers = {}

            def _secret(value: str | None) -> str | None:
                if not value:
                    return None
                try:
                    return decrypt(value)
                except Exception:
                    return None

            out.append(
                {
                    "id": row.id,
                    "url": row.url,
                    "method": row.method,
                    "expected_status": row.expected_status,
                    "timeout": row.timeout,
                    "headers": {str(k): str(v) for k, v in headers.items()},
                    "auth_type": row.auth_type or "none",
                    "auth_username": row.auth_username,
                    "auth_password": _secret(row.auth_password_enc),
                    "auth_token": _secret(row.auth_token_enc),
                    "request_body": row.request_body,
                    "response_contains": row.response_contains,
                }
            )
        return out
    finally:
        db.close()


async def _probe_one(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    expected_status: int,
    timeout: int,
    headers: dict[str, str],
    auth_type: str,
    auth_username: str | None,
    auth_password: str | None,
    auth_token: str | None,
    request_body: str | None,
    response_contains: str | None,
) -> dict:
    started = time.monotonic()
    try:
        validate_outbound_url(url)
        auth = None
        if auth_type == "basic" and auth_username and auth_password:
            auth = httpx.BasicAuth(auth_username, auth_password)
        elif auth_type == "bearer" and auth_token:
            headers = {**headers, "Authorization": f"Bearer {auth_token}"}
        resp = await client.request(
            method,
            url,
            timeout=timeout,
            follow_redirects=False,
            headers=headers or None,
            content=request_body or None,
            auth=auth,
        )
        latency = int((time.monotonic() - started) * 1000)
        up = 1 if resp.status_code == expected_status else 0
        content_error = None
        if up and response_contains:
            content_error = (
                "响应内容未包含预期文本" if response_contains not in resp.text else None
            )
            up = 0 if content_error else up
        return {
            "up": up,
            "status_code": resp.status_code,
            "latency_ms": latency,
            "error": None
            if up
            else (
                content_error
                or f"状态码 {resp.status_code} 与预期 {expected_status} 不符"
            ),
        }
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        return {
            "up": 0,
            "status_code": None,
            "latency_ms": latency,
            "error": str(exc)[:200],
        }


def _filter_debounced(
    results: list[tuple[int, dict]], *, immediate: bool
) -> list[tuple[int, dict]]:
    """探测结果消抖:周期轮里首次失败沿用上一轮落库结果(不写不翻灯),
    连续第 2 次失败才落 down;恢复 up 单次即生效。

    ``immediate=True``(接口创建/编辑后的即时探测)不消抖:用户刚保存的配置
    应立刻看到真实结果,此时失败本来就是可信信号。成功结果同样会清零
    连续失败计数。周期轮还负责清理已删除/停用接口的残留计数(防无界增长)。
    """
    kept: list[tuple[int, dict]] = []
    fresh_ids: set[int] = set()
    for interface_id, result in results:
        fresh_ids.add(interface_id)
        if result["up"] == 1:
            _fail_streaks.pop(interface_id, None)
            kept.append((interface_id, result))
            continue
        fails = _fail_streaks.get(interface_id, 0) + 1
        _fail_streaks[interface_id] = fails
        if immediate or fails >= PROBE_CONFIRM_SCANS:
            kept.append((interface_id, result))
    if not immediate:
        for interface_id in [k for k in _fail_streaks if k not in fresh_ids]:
            _fail_streaks.pop(interface_id, None)
    return kept


def _upsert_probes(results: list[tuple[int, dict]]) -> None:
    """在一个事务中批量 upsert 本轮接口探测结果。"""
    db = SessionLocal()
    try:
        interface_ids = [interface_id for interface_id, _result in results]
        existing = {
            row.interface_id: row
            for row in (
                db.query(InterfaceProbe)
                .filter(InterfaceProbe.interface_id.in_(interface_ids))
                .all()
            )
        }
        checked_at = datetime.now(timezone.utc)
        for interface_id, result in results:
            row = existing.get(interface_id)
            if row is None:
                row = InterfaceProbe(interface_id=interface_id)
                db.add(row)
            row.up = result["up"]
            row.status_code = result["status_code"]
            row.latency_ms = result["latency_ms"]
            row.error = result["error"]
            row.checked_at = checked_at
        db.commit()
    finally:
        db.close()


async def _probe_interfaces(interfaces: list[dict]) -> list[tuple[int, dict]]:
    """并发探测一批接口配置,返回 (interface_id, result) 列表。"""
    sem = asyncio.Semaphore(INTERFACE_PROBE_CONCURRENCY)
    # Do not honor process-wide HTTP(S)_PROXY/NO_PROXY values here. A proxy can
    # otherwise turn a validated direct request into an unexpected SSRF path
    # and can make probe results depend on deployment shell configuration.
    async with httpx.AsyncClient(trust_env=False) as client:

        async def run(item: dict) -> tuple[int, dict]:
            async with sem:
                result = await _probe_one(
                    client,
                    item["url"],
                    item["method"],
                    item["expected_status"],
                    item["timeout"],
                    item["headers"],
                    item["auth_type"],
                    item["auth_username"],
                    item["auth_password"],
                    item["auth_token"],
                    item["request_body"],
                    item["response_contains"],
                )
                return item["id"], result

        return await asyncio.gather(*[run(item) for item in interfaces])


async def _probe_cycle() -> None:
    loop = asyncio.get_running_loop()
    interfaces = await loop.run_in_executor(None, _fetch_interfaces)
    if not interfaces:
        return
    results = await _probe_interfaces(interfaces)
    results = _filter_debounced(results, immediate=False)
    await loop.run_in_executor(None, _upsert_probes, results)


async def _probe_now(interface_ids: list[int]) -> None:
    """立即探测指定接口(创建/编辑后调用):不等周期边界,不消抖。"""
    loop = asyncio.get_running_loop()
    interfaces = await loop.run_in_executor(None, _fetch_interfaces, interface_ids)
    if not interfaces:
        return
    results = await _probe_interfaces(interfaces)
    results = _filter_debounced(results, immediate=True)
    await loop.run_in_executor(None, _upsert_probes, results)


def trigger_immediate_probe(interface_ids: list[int]) -> None:
    """从同步路由线程触发立即探测;探测器未运行时静默跳过(周期循环兑底)。

    新建/编辑接口后不必等下一个 60s 周期才出首个结果——期间「尚未完成首次
    探测」会按异常计入业务健康,卡片先 degraded 再恢复。
    """
    loop = _loop
    if loop is None or loop.is_closed():
        return
    ids = [interface_id for interface_id in interface_ids if interface_id]
    if not ids:
        return
    asyncio.run_coroutine_threadsafe(_probe_now(ids), loop)


async def run_interface_probe_loop():
    global _loop
    _loop = asyncio.get_running_loop()
    logger.info("Interface probe started (interval=%ds)", INTERFACE_PROBE_INTERVAL)
    while True:
        try:
            await _probe_cycle()
        except asyncio.CancelledError:
            raise  # 正常关闭
        except Exception:
            logger.exception("Interface probe cycle failed")
        await asyncio.sleep(INTERFACE_PROBE_INTERVAL)
