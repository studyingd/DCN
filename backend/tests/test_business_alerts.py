"""业务告警(evaluate_business_alerts)的虚机口径与公共判定助手。

钉住 2026-09-17 的修复:业务告警评估此前完全不含 PVE 虚拟机——纯虚机业务
total=0 永远 value=0(正常),全部虚机停机也不告警;修复后与业务面板
(routers/businesses)同口径:PVE 不可达=状态未知,不计入健康分母。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.business import Business, BusinessPveGuest, BusinessServer
from app.models.device import Device
from app.models.pve_connection import PveConnection
from app.models.rack import Rack
from app.models.room import Room
from app.services import alerts, pve_guest_status

# ── 公共助手:纯内存,直接构造快照 ──


def test_judged_guest_health_excludes_unreachable():
    """不可达=状态未知,不计入分母;可达的按 (online, reachable) 计。"""
    states = [(1, 1), (0, 1), (0, 0), (1, 0)]
    total, online = pve_guest_status.judged_guest_health(states)
    assert (total, online) == (2, 1)


def test_judged_guest_health_empty():
    assert pve_guest_status.judged_guest_health([]) == (0, 0)


def test_guest_link_state_from_snapshots(monkeypatch):
    """(online, reachable) 只读内存快照;类型不一致按未知处理。"""
    monkeypatch.setattr(pve_guest_status, "_guests", {})
    monkeypatch.setattr(pve_guest_status, "_connection_states", {})
    pve_guest_status._guests[(1, 101)] = {
        "guest_type": "qemu",
        "status": "running",
    }
    pve_guest_status._connection_states[1] = {"reachable": True}

    # 可达 + 运行中 → 在线
    assert pve_guest_status.guest_link_state(1, "qemu", 101) == (1, 1)
    # 快照 guest_type 与关联行不一致(vmid 复用)→ 未知,不计入
    assert pve_guest_status.guest_link_state(1, "lxc", 101) == (0, 1)
    # 连接不可达 → 状态未知
    pve_guest_status._connection_states[1] = {"reachable": False}
    assert pve_guest_status.guest_link_state(1, "qemu", 101) == (0, 0)
    # 无任何快照 → 未知
    assert pve_guest_status.guest_link_state(9, "qemu", 999) == (0, 0)


# ── evaluate_business_alerts:资源构造口径 ──


def _create_business_with_guest(
    suffix: str,
) -> tuple[Business, BusinessPveGuest, PveConnection]:
    """纯虚机业务 + 真实 PveConnection 行(connection_id 有 FK 约束)。"""
    db = SessionLocal()
    try:
        conn = PveConnection(
            name=f"biz_alert_conn_{suffix}",
            host="127.0.0.1",
            token_id=f"root@pam!{suffix}",
            token_secret_enc="encrypted-placeholder",
            enabled=1,
        )
        business = Business(name=f"biz_alert_{suffix}")
        db.add_all([conn, business])
        db.flush()
        link = BusinessPveGuest(
            business_id=business.id,
            connection_id=conn.id,
            guest_type="qemu",
            vmid=101,
            guest_name="vm-101",
        )
        db.add(link)
        db.commit()
        for obj in (business, link, conn):
            db.refresh(obj)
        return business, link, conn
    finally:
        db.close()


def _cleanup_business_with_guest(business_id: int, link_id: int, conn_id: int) -> None:
    db = SessionLocal()
    try:
        db.query(BusinessPveGuest).filter(BusinessPveGuest.id == link_id).delete(
            synchronize_session=False
        )
        db.query(Business).filter(Business.id == business_id).delete(
            synchronize_session=False
        )
        db.query(PveConnection).filter(PveConnection.id == conn_id).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def _run_evaluate(monkeypatch, captured: list) -> dict[int, dict]:
    """跑一轮评估,捕获 resources(不落告警事件),按 business_id 返回。

    评估会对全库业务出资源;这里只关心自己创建的那条,其余按 business_id
    索引后忽略——不落库,不影响告警中心。
    """

    def fake(resources: list) -> None:
        captured.extend(resources)

    monkeypatch.setattr(alerts, "_evaluate_resource_rules", fake)
    alerts.evaluate_business_alerts()
    return {item["business_id"]: item for item in captured}


def test_guest_only_business_alerts_when_guest_stopped(monkeypatch):
    """纯虚机业务:guest 停机 → value=1(异常);旧实现恒为 0。"""
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    business, link, conn = _create_business_with_guest(suffix)
    try:
        captured: list = []
        # 可达但已停止:online=0, reachable=1 → 计入分母且不在线。
        monkeypatch.setattr(alerts, "guest_link_state", lambda cid, gtype, vmid: (0, 1))
        item = _run_evaluate(monkeypatch, captured)[business.id]
        assert item["metric"] == "business_status"
        assert item["value"] == 1
        assert "服务器 0/1 在线" in item["message"]

        # guest 运行中 → 恢复正常(value=0)
        captured.clear()
        monkeypatch.setattr(alerts, "guest_link_state", lambda cid, gtype, vmid: (1, 1))
        assert _run_evaluate(monkeypatch, captured)[business.id]["value"] == 0

        # PVE 不可达 → 状态未知,不计入分母,不告警
        captured.clear()
        monkeypatch.setattr(alerts, "guest_link_state", lambda cid, gtype, vmid: (0, 0))
        item = _run_evaluate(monkeypatch, captured)[business.id]
        assert item["value"] == 0
        assert "另有 1 台虚拟机状态未知" in item["message"]
    finally:
        _cleanup_business_with_guest(business.id, link.id, conn.id)


def test_business_alert_device_path_unchanged(monkeypatch):
    """设备路径回归:仅设备且离线 → value=1;在线 → value=0。"""
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    db = SessionLocal()
    room = Room(name=f"biz_alert_room_{suffix}")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"biz_alert_rack_{suffix}", type="cabinet")
    db.add(rack)
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=f"biz_alert_dev_{suffix}",
        type="server",
        ip_address=f"192.0.2.{(int(suffix[-3:]) % 200) + 10}",
        status="offline",
    )
    business = Business(name=f"biz_alert_dev_biz_{suffix}")
    db.add_all([device, business])
    db.flush()
    link = BusinessServer(business_id=business.id, device_id=device.id)
    db.add(link)
    db.commit()
    try:
        captured: list = []
        # monitor 内存缓存为空 → 回退 DB status(offline)。
        monkeypatch.setattr(alerts, "get_latest_statuses", lambda: {})
        item = _run_evaluate(monkeypatch, captured)[business.id]
        assert item["value"] == 1
        assert "服务器 0/1 在线" in item["message"]

        db2 = SessionLocal()
        try:
            db2.query(Device).filter(Device.id == device.id).update(
                {"status": "online"}
            )
            db2.commit()
        finally:
            db2.close()
        captured.clear()
        assert _run_evaluate(monkeypatch, captured)[business.id]["value"] == 0
    finally:
        db.rollback()
        db.query(BusinessServer).filter(BusinessServer.id == link.id).delete(
            synchronize_session=False
        )
        db.query(Business).filter(Business.id == business.id).delete(
            synchronize_session=False
        )
        db.query(Device).filter(Device.id == device.id).delete(
            synchronize_session=False
        )
        db.query(Rack).filter(Rack.id == rack.id).delete(synchronize_session=False)
        db.query(Room).filter(Room.id == room.id).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_sync_business_guest_names_writes_snapshot(monkeypatch):
    """名称同步助手:可达且名称变化时落库;不可达/同名不动。"""
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    business, link, conn = _create_business_with_guest(suffix)
    try:
        monkeypatch.setattr(pve_guest_status, "_guests", {})
        monkeypatch.setattr(pve_guest_status, "_connection_states", {})
        pve_guest_status._guests[(link.connection_id, 101)] = {
            "guest_type": "qemu",
            "status": "running",
            "name": "renamed-vm",
        }
        pve_guest_status._connection_states[link.connection_id] = {"reachable": True}
        changed = pve_guest_status._sync_business_guest_names_sync()
        assert changed == 1
        db = SessionLocal()
        try:
            row = (
                db.query(BusinessPveGuest)
                .filter(BusinessPveGuest.id == link.id)
                .first()
            )
            assert row.guest_name == "renamed-vm"
        finally:
            db.close()

        # 再跑一轮:名称未变 → 0 条写入
        assert pve_guest_status._sync_business_guest_names_sync() == 0

        # 连接不可达 → 不改名
        pve_guest_status._connection_states[link.connection_id] = {"reachable": False}
        pve_guest_status._guests[(link.connection_id, 101)]["name"] = "ghost"
        assert pve_guest_status._sync_business_guest_names_sync() == 0
    finally:
        _cleanup_business_with_guest(business.id, link.id, conn.id)


# ── 独立评估循环(2026-09-17 起) ──


async def _fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


async def _run_loop_rounds(rounds: int) -> None:
    """跑 run_business_alert_loop 若干轮(评估经 to_thread,循环间隔归零)。"""
    task = asyncio.create_task(alerts.run_business_alert_loop())
    for _ in range(rounds):
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_business_alert_loop_evaluates_independently(monkeypatch):
    """独立评估循环:不依赖指标采集周期(METRICS_ENABLED=false 也能评估)。

    旧挂靠(metrics_collector 周期内调)在纯接口监控部署下会让业务告警
    静默失效(2026-09-17 迁出)。
    """
    calls: list = []

    def fake_evaluate() -> None:
        calls.append(1)

    monkeypatch.setattr(alerts, "evaluate_business_alerts", fake_evaluate)
    # alerts 在模块导入时已把常量拷贝进来(评估回溯也用它),patch alerts 侧
    monkeypatch.setattr(alerts, "BUSINESS_ALERT_INTERVAL", 0)
    asyncio.run(_run_loop_rounds(3))
    assert len(calls) >= 1


def test_business_alert_loop_survives_evaluation_error(monkeypatch):
    """评估抛异常不退出循环(异常被吞掉,下一轮照常)。"""
    calls: list = []

    def fake_evaluate() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(alerts, "evaluate_business_alerts", fake_evaluate)
    monkeypatch.setattr(alerts, "BUSINESS_ALERT_INTERVAL", 0)
    asyncio.run(_run_loop_rounds(3))
    assert len(calls) >= 2  # 第一轮抛错后循环仍在跑


# ── 异常明细 + sustain 回溯(2026-09-18) ──


def _create_business_with_iface(suffix: str):
    """纯接口业务:接口 + 探测行(探测结果由用例直接给定)。"""
    from app.models.business import BusinessInterface
    from app.models.service_interface import ServiceInterface

    db = SessionLocal()
    try:
        business = Business(name=f"biz_iface_{suffix}")
        iface = ServiceInterface(
            name="主页面", url="http://192.0.2.10/health", enabled=1
        )
        db.add_all([business, iface])
        db.flush()
        db.add(BusinessInterface(business_id=business.id, interface_id=iface.id))
        db.commit()
        for obj in (business, iface):
            db.refresh(obj)
        return business, iface
    finally:
        db.close()


def _cleanup_business_with_iface(business_id: int, iface_id: int) -> None:
    from app.models.service_interface import ServiceInterface

    db = SessionLocal()
    try:
        db.query(Business).filter(Business.id == business_id).delete(
            synchronize_session=False
        )
        db.query(ServiceInterface).filter(ServiceInterface.id == iface_id).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def _set_probe(iface_id: int, *, up: int, status_code: int | None, error: str | None):
    from app.models.service_interface import InterfaceProbe

    db = SessionLocal()
    try:
        row = (
            db.query(InterfaceProbe)
            .filter(InterfaceProbe.interface_id == iface_id)
            .first()
        )
        if row is None:
            row = InterfaceProbe(interface_id=iface_id)
            db.add(row)
        row.up = up
        row.status_code = status_code
        row.error = error
        row.checked_at = datetime.now(timezone.utc) - timedelta(seconds=20)
        db.commit()
    finally:
        db.close()


def test_interface_failure_message_carries_name_and_reason(monkeypatch):
    """接口异常明细:直接点名接口与探测结论(状态码/错误文本),
    不再只给"接口 0/1 正常"的汇总计数;探测时刻回溯 sampled_at。"""
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    business, iface = _create_business_with_iface(suffix)
    try:
        _set_probe(iface.id, up=0, status_code=404, error="状态码 404 与预期 200 不符")
        captured: list = []
        item = _run_evaluate(monkeypatch, captured)[business.id]
        assert item["value"] == 1
        assert "接口「主页面」状态码 404 与预期 200 不符" in item["message"]
        assert "异常明细" in item["message"]
        # 明细另起一行:汇总句与明细之间是换行符,多条明细各自一行
        assert "\n异常明细：" in item["message"]
        # 探测时刻(20s 前)作为 sampled_at 回溯,sustain 从真实失败时刻起算
        assert item["sampled_at"] is not None
        age = (datetime.now(timezone.utc) - item["sampled_at"]).total_seconds()
        assert 15 <= age <= 30

        # 恢复后:明细消失,sampled_at 回到 None
        _set_probe(iface.id, up=1, status_code=200, error=None)
        captured.clear()
        item = _run_evaluate(monkeypatch, captured)[business.id]
        assert item["value"] == 0
        assert "异常明细" not in item["message"]
        assert item["sampled_at"] is None
    finally:
        _cleanup_business_with_iface(business.id, iface.id)


def test_interface_without_probe_notes_missing(monkeypatch):
    """关联了但从未探测过:明细指出暂无探测结果(计入分母,天然异常)。"""
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "") + "x"
    business, iface = _create_business_with_iface(suffix)
    try:
        captured: list = []
        item = _run_evaluate(monkeypatch, captured)[business.id]
        assert item["value"] == 1
        assert "接口「主页面」暂无探测结果" in item["message"]
        assert item["sampled_at"] is None  # 无证据时刻,不回溯
    finally:
        _cleanup_business_with_iface(business.id, iface.id)


def test_server_failure_message_names_device(monkeypatch):
    """服务器离线明细:点名设备名与 IP。"""
    from app.models.device import Device

    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "") + "s"
    db = SessionLocal()
    room = Room(name=f"biz_srv_room_{suffix}")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"biz_srv_rack_{suffix}", type="cabinet")
    db.add(rack)
    db.flush()
    business = Business(name=f"biz_srv_{suffix}")
    server = Device(
        rack_id=rack.id,
        name=f"biz-srv-{suffix}",
        type="server",
        ip_address="192.0.2.30",
    )
    db.add_all([business, server])
    db.flush()
    db.add(BusinessServer(business_id=business.id, device_id=server.id))
    db.commit()
    biz_id, server_id, room_id, rack_id = business.id, server.id, room.id, rack.id
    db.close()

    try:
        # 设备状态离线(经 get_latest_statuses 快照,不改设备行本身)
        monkeypatch.setattr(
            alerts, "get_latest_statuses", lambda: {server_id: "offline"}
        )
        captured: list = []
        item = _run_evaluate(monkeypatch, captured)[biz_id]
        assert item["value"] == 1
        assert f"服务器 biz-srv-{suffix}(192.0.2.30) 离线" in item["message"]
        # 设备侧无逐台时间戳,不回溯
        assert item["sampled_at"] is None
    finally:
        db = SessionLocal()
        try:
            db.query(Business).filter(Business.id == biz_id).delete(
                synchronize_session=False
            )
            db.query(Device).filter(Device.id == server_id).delete(
                synchronize_session=False
            )
            db.query(Rack).filter(Rack.id == rack_id).delete(synchronize_session=False)
            db.query(Room).filter(Room.id == room_id).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


def test_guest_failure_message_and_backtrack(monkeypatch):
    """虚机离线明细点名虚机,快照时刻参与 sampled_at 回溯。"""
    import time as _time

    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "") + "g"
    business, link, conn = _create_business_with_guest(suffix)
    try:
        # 快照:连接可达、guest 停机,checked_at = 25s 前
        pve_guest_status._guests[(conn.id, link.vmid)] = {
            "guest_type": "qemu",
            "status": "stopped",
        }
        pve_guest_status._connection_states[conn.id] = {
            "reachable": True,
            "checked_at": _time.time() - 25,
        }
        captured: list = []
        item = _run_evaluate(monkeypatch, captured)[business.id]
        assert item["value"] == 1
        assert "虚拟机 vm-101 离线或未运行" in item["message"]
        assert item["sampled_at"] is not None
        age = (datetime.now(timezone.utc) - item["sampled_at"]).total_seconds()
        assert 20 <= age <= 30
    finally:
        pve_guest_status._guests.pop((conn.id, link.vmid), None)
        pve_guest_status._connection_states.pop(conn.id, None)
        _cleanup_business_with_guest(business.id, link.id, conn.id)
