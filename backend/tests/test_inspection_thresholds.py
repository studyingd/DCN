"""GET /api/inspection/thresholds —— 分指标配色阈值的出口契约。

同一个百分比数字在项目里曾有三套判定口径:后端巡检的分指标 `THRESHOLDS`
(cpu 70/85、memory 75/90、disk 80/95、failed_services 1/3)、前端指标页一刀切的
70/90、以及告警规则里的条件阈值。这个端点负责把第一套暴露出去,让指标监控页 /
PVE 页 / 容器页按 item_type 查表配色,而不是各自再写一份数字。

这里钉住三件事:
  1. 数值与 `inspection_parser.THRESHOLDS` 逐项相等 —— 刻意不写死数字,以后改阈值表
     不该让测试变成阻力;但路由里若偷偷复制了一份旧数字,仍会在这里红掉;
  2. 权限口径是 `device:view` 而非 `automation:manage` —— 这是本次设计的核心,
     只有 device:view 的角色必须能读到 200,无任何权限则 403;
  3. 响应形状不多不少 —— 防止将来有人往里塞不该暴露的字段。
"""

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.device import Device
from app.models.rack import Rack
from app.models.role import Role
from app.models.room import Room
from app.models.user import User
from app.services.auth import create_access_token
from app.services.inspection_parser import THRESHOLDS

client = TestClient(app)

URL = "/api/inspection/thresholds"

# 契约约定的四个 item_type。THRESHOLDS 增删项时这里要同步,属于有意为之的「冻结」。
EXPECTED_ITEM_TYPES = {"cpu", "memory", "disk", "failed_services"}


def _fixture(permissions):
    """建一套 role/room/rack/user/device 与对应 token,权限由入参决定。

    阈值端点本身不读设备,但沿用 test_automation.py 的建数据风格:一方面与其它用例
    一致,另一方面 device 的存在能顺带证明这个端点不做设备级过滤。
    """
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    role = Role(
        name=f"threshold_role_{suffix}",
        permissions=json.dumps(permissions),
        device_scope="all",
    )
    room = Room(name=f"threshold_room_{suffix}")
    db.add_all([role, room])
    db.flush()
    rack = Rack(room_id=room.id, name=f"threshold_rack_{suffix}", type="cabinet")
    user = User(
        username=f"threshold_user_{suffix}",
        password="irrelevant",
        role="viewer",
        is_active=1,
        role_id=role.id,
    )
    db.add_all([rack, user])
    db.flush()
    device = Device(
        rack_id=rack.id,
        name=f"threshold_device_{suffix}",
        type="server",
        ip_address="192.0.2.91",
        status="online",
        os_system="Linux",
    )
    db.add(device)
    db.commit()
    token = create_access_token(
        user_id=user.id,
        username=user.username,
        role=user.role,
        permissions=permissions,
        device_scope="all",
    )
    return db, role, room, rack, user, device, {"Authorization": f"Bearer {token}"}


def _cleanup(db, role, room, rack, user, device):
    """device 必须删干净:test_metrics_api.py::test_list_metrics 会数全库设备。"""
    db.delete(device)
    db.delete(user)
    db.delete(rack)
    db.delete(room)
    db.delete(role)
    db.commit()
    db.close()


def test_thresholds_match_parser_table():
    """返回值与 THRESHOLDS 逐项相等(不写死数字),int 阈值按契约升格为 float。"""
    db, role, room, rack, user, device, headers = _fixture(
        ["device:view", "automation:manage"]
    )
    try:
        resp = client.get(URL, headers=headers)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert set(items) == set(THRESHOLDS)
        for item_type, thresholds in THRESHOLDS.items():
            assert items[item_type]["warning"] == float(thresholds["warning"])
            assert items[item_type]["critical"] == float(thresholds["critical"])
    finally:
        _cleanup(db, role, room, rack, user, device)


def test_thresholds_response_shape_is_frozen():
    """响应里没有多余的 key —— 顶层只有 items,每项只有 warning/critical。"""
    db, role, room, rack, user, device, headers = _fixture(["device:view"])
    try:
        payload = client.get(URL, headers=headers).json()
        assert set(payload) == {"items"}
        assert set(payload["items"]) == EXPECTED_ITEM_TYPES
        for entry in payload["items"].values():
            assert set(entry) == {"warning", "critical"}
    finally:
        _cleanup(db, role, room, rack, user, device)


def test_device_view_only_role_can_read_thresholds():
    """核心设计:只有 device:view、没有 automation:manage 的角色也必须读到 200。

    消费方是指标监控页 / PVE 页 / 容器页,这些页面的用户不一定持有
    automation:manage。这条红了就说明权限被「顺手统一」了,前端会整片 403。
    """
    db, role, room, rack, user, device, headers = _fixture(["device:view"])
    try:
        # 先把前提钉死:这个角色确实没有 automation:manage,否则本用例毫无意义。
        assert "automation:manage" not in json.loads(role.permissions)
        resp = client.get(URL, headers=headers)
        assert resp.status_code == 200
        assert set(resp.json()["items"]) == EXPECTED_ITEM_TYPES
    finally:
        _cleanup(db, role, room, rack, user, device)


def test_thresholds_reject_role_without_permission():
    """无任何权限 → 403,端点不是匿名可读的。"""
    db, role, room, rack, user, device, headers = _fixture([])
    try:
        resp = client.get(URL, headers=headers)
        assert resp.status_code == 403
    finally:
        _cleanup(db, role, room, rack, user, device)
