"""backfill_os_system.py 的行为测试。

探测一律用桩替换，绝不真连设备。重点覆盖四件事：
  1. 只回填 os_system 缺失的行，已有值不被覆盖（含探测期间的并发写入）；
  2. dry-run 报告结果但不写库；
  3. 单台设备探测失败 / 无 IP 不拖垮整批；
  4. 探测不出结果时不写库，留给下一次重跑。

backfill 的用例一律通过 monkeypatch 注入目标列表，只断言自己创建的那几台设备，
对测试库里历史残留的 cloud_server 行不敏感（与 test_metrics_collector 同款约定）。

这个脚本存在的原因：设备表单早期把「支持 Windows 远程」写死成 server/host，
云服务器跳过了 OS 自动探测，os_system 留空，进而让监控/巡检/自动化错选 SSH 通道。
"""

import asyncio

import pytest
from sqlalchemy.orm import Session

import backfill_os_system as bos
from app.database import Base, SessionLocal, engine
from app.models.device import OPS_TARGET_TYPES, Device
from app.models.rack import Rack
from app.models.room import Room
from app.services.os_detect import OSDetectResult

# ── 脚手架（与 test_metrics_collector 同款，自带清理）──


def _new_rack(db: Session, tag: str) -> tuple[Room, Rack]:
    room = Room(name=f"bf-{tag}-room", location="L1")
    db.add(room)
    db.flush()
    rack = Rack(room_id=room.id, name=f"bf-{tag}-rack", type="cabinet")
    db.add(rack)
    db.flush()
    return room, rack


def _make_device(db: Session, rack_id: int, **fields) -> Device:
    defaults = dict(
        type="cloud_server",
        status="offline",
        ip_address="203.0.113.20",
        os_system=None,
    )
    defaults.update(fields)
    device = Device(rack_id=rack_id, **defaults)
    db.add(device)
    db.commit()
    return device


def _cleanup(
    db: Session, device_ids: list[int], rack_ids: list[int], room_ids: list[int]
) -> None:
    db.query(Device).filter(Device.id.in_(device_ids)).delete(synchronize_session=False)
    db.query(Rack).filter(Rack.id.in_(rack_ids)).delete(synchronize_session=False)
    db.query(Room).filter(Room.id.in_(room_ids)).delete(synchronize_session=False)
    db.commit()


@pytest.fixture
def stub_detect(monkeypatch):
    """替换 detect_os：返回可配置的探测结果，并记录被探测的 IP。"""

    class Stub:
        results: dict[str, str] = {}  # ip -> os_system；未命中返回 "unknown"
        versions: dict[str, str] = {}  # ip -> os_version(精确名);不设则为 ""
        probed: list[str] = []
        raise_for: set[str] = set()

    stub = Stub()

    async def fake_detect(ip_address, **kwargs):
        stub.probed.append(ip_address)
        if ip_address in stub.raise_for:
            raise OSError("connection reset by peer")
        result = OSDetectResult(ip_address=ip_address)
        result.os_system = stub.results.get(ip_address, "unknown")
        result.os_version = stub.versions.get(ip_address, "")
        return result

    monkeypatch.setattr(bos, "detect_os", fake_detect)
    return stub


def _inject_targets(monkeypatch, devices: list[Device]) -> None:
    """把 backfill 的目标集固定成用例自建设备，屏蔽库里的历史残留行。

    这样 count / probed 的断言才是确定性的；真实的筛选逻辑由
    test_load_targets_only_picks_missing_os_system 单独覆盖。
    """
    monkeypatch.setattr(
        bos, "_load_targets", lambda db, types, refine=False: list(devices)
    )


def _reload_os(db: Session, device_id: int) -> str | None:
    """绕开 MySQL REPEATABLE READ 的旧快照读回真实值。

    backfill 用的是自己的 SessionLocal 会话；本用例的 db 会话如果事务还开着，
    快照就停在它开启的那一刻，expire_all() 只过期对象、不刷新快照，
    于是永远读不到另一个会话已提交的写入。先结束事务再查。
    """
    db.commit()
    db.expire_all()
    row = db.query(Device).filter(Device.id == device_id).first()
    return row.os_system if row else None


# ── _load_targets：筛选逻辑 ──


def test_load_targets_only_picks_missing_os_system():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "targets")
    ids: list[int] = []
    try:
        empty = _make_device(db, rack.id, name="bf-empty", os_system=None)
        blank = _make_device(db, rack.id, name="bf-blank", os_system="   ")
        filled = _make_device(
            db, rack.id, name="bf-filled", os_system="Ubuntu 22.04.3 LTS"
        )
        server = _make_device(
            db, rack.id, name="bf-server", type="server", os_system=None
        )
        ids = [empty.id, blank.id, filled.id, server.id]

        picked = {d.id for d in bos._load_targets(db, ("cloud_server",), False)}
        # NULL 与空白串都算缺失
        assert {empty.id, blank.id} <= picked
        assert filled.id not in picked
        assert server.id not in picked  # 类型过滤生效

        picked_all = {d.id for d in bos._load_targets(db, OPS_TARGET_TYPES, False)}
        assert {empty.id, blank.id, server.id} <= picked_all
        # 已有值的行绝不进目标集
        assert filled.id not in picked_all
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


# ── backfill：写入行为 ──


def test_backfill_writes_detected_os(stub_detect, monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "write")
    ids: list[int] = []
    try:
        target = _make_device(
            db, rack.id, name="bf-win", ip_address="203.0.113.31", os_system=None
        )
        ids = [target.id]
        stub_detect.results = {"203.0.113.31": "windows"}
        _inject_targets(monkeypatch, [target])

        count = asyncio.run(bos.backfill(OPS_TARGET_TYPES, 2, dry_run=False))

        assert count == 1
        assert _reload_os(db, target.id) == "windows"
        assert stub_detect.probed == ["203.0.113.31"]
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


def test_backfill_dry_run_does_not_write(stub_detect, monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "dry")
    ids: list[int] = []
    try:
        target = _make_device(
            db, rack.id, name="bf-dry", ip_address="203.0.113.32", os_system=None
        )
        ids = [target.id]
        stub_detect.results = {"203.0.113.32": "linux"}
        _inject_targets(monkeypatch, [target])

        count = asyncio.run(bos.backfill(OPS_TARGET_TYPES, 2, dry_run=True))

        # dry-run 仍然报告“可回填”，但不落库
        assert count == 1
        assert _reload_os(db, target.id) is None
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


def test_backfill_never_overwrites_existing_value(stub_detect, monkeypatch):
    """探测期间用户可能在界面上手工填了值——写入前必须复查。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "nooverwrite")
    ids: list[int] = []
    try:
        target = _make_device(
            db, rack.id, name="bf-race", ip_address="203.0.113.33", os_system=None
        )
        ids = [target.id]
        _inject_targets(monkeypatch, [target])

        async def detect_then_fill(ip_address, **kwargs):
            # 模拟“探测进行中，另一个会话把 os_system 填好了”
            other = SessionLocal()
            try:
                row = other.query(Device).filter(Device.id == target.id).first()
                row.os_system = "Rocky Linux 9.4"
                other.commit()
            finally:
                other.close()
            result = OSDetectResult(ip_address=ip_address)
            result.os_system = "linux"
            return result

        monkeypatch.setattr(bos, "detect_os", detect_then_fill)

        asyncio.run(bos.backfill(OPS_TARGET_TYPES, 1, dry_run=False))

        # 手工填写的精确值胜出，不被粗粒度的 "linux" 覆盖
        assert _reload_os(db, target.id) == "Rocky Linux 9.4"
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


def test_backfill_skips_device_without_ip(stub_detect, monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "noip")
    ids: list[int] = []
    try:
        no_ip = _make_device(db, rack.id, name="bf-noip", ip_address=None)
        ids = [no_ip.id]
        _inject_targets(monkeypatch, [no_ip])

        count = asyncio.run(bos.backfill(OPS_TARGET_TYPES, 2, dry_run=False))

        assert count == 0
        # 没有 IP 就不该发起任何探测
        assert stub_detect.probed == []
        assert _reload_os(db, no_ip.id) is None
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


def test_backfill_survives_probe_failure(stub_detect, monkeypatch):
    """单台探测抛异常不能中断整批。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "fail")
    ids: list[int] = []
    try:
        bad = _make_device(
            db, rack.id, name="bf-bad", ip_address="203.0.113.41", os_system=None
        )
        good = _make_device(
            db, rack.id, name="bf-good", ip_address="203.0.113.42", os_system=None
        )
        ids = [bad.id, good.id]
        stub_detect.raise_for = {"203.0.113.41"}
        stub_detect.results = {"203.0.113.42": "linux"}
        _inject_targets(monkeypatch, [bad, good])

        count = asyncio.run(bos.backfill(OPS_TARGET_TYPES, 2, dry_run=False))

        assert count == 1
        assert _reload_os(db, bad.id) is None
        assert _reload_os(db, good.id) == "linux"
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


def test_backfill_ignores_unidentified_result(stub_detect, monkeypatch):
    """探测不出结果时不写库，留给下一次重跑。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "unknown")
    ids: list[int] = []
    try:
        target = _make_device(
            db, rack.id, name="bf-unknown", ip_address="203.0.113.51", os_system=None
        )
        ids = [target.id]
        stub_detect.results = {}  # 默认 "unknown"
        _inject_targets(monkeypatch, [target])

        count = asyncio.run(bos.backfill(OPS_TARGET_TYPES, 2, dry_run=False))

        assert count == 0
        assert _reload_os(db, target.id) is None
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


def test_backfill_reports_zero_when_nothing_to_do(stub_detect, monkeypatch, capsys):
    """无目标时早退，不发起任何探测。"""
    monkeypatch.setattr(bos, "_load_targets", lambda db, types, refine=False: [])

    count = asyncio.run(bos.backfill(OPS_TARGET_TYPES, 2, dry_run=False))

    assert count == 0
    assert "无需处理" in capsys.readouterr().out
    assert stub_detect.probed == []


# ── CLI ──


def test_cli_type_choices_track_managed_types():
    """--type 的可选值必须跟着 OPS_TARGET_TYPES 走。

    否则新增类型后脚本会拒收它，而报错看起来像是用户输错了。
    """
    parser = bos._build_parser()
    type_action = next(a for a in parser._actions if a.dest == "type")
    assert set(type_action.choices) == set(OPS_TARGET_TYPES)


def test_cli_defaults():
    args = bos._build_parser().parse_args([])
    assert args.type is None
    assert args.concurrency == 2
    assert args.dry_run is False
    assert args.yes is False


def test_cli_accepts_repeated_type_flags():
    args = bos._build_parser().parse_args(
        ["--type", "cloud_server", "--type", "host", "--dry-run", "--yes"]
    )
    assert args.type == ["cloud_server", "host"]
    assert args.dry_run is True
    assert args.yes is True


def test_cli_rejects_unmanaged_type():
    """已下线的网络设备类型不得被接受。"""
    parser = bos._build_parser()
    for bad in ("switch", "router", "firewall"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--type", bad])


# ── --refine 模式:粗粒度值升级成精确版本名 ──


def test_load_targets_refine_picks_only_coarse_values():
    """refine 只挑 linux/windows/unknown 粗值;精确名与空缺都不动。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "refine-pick")
    ids: list[int] = []
    try:
        coarse = _make_device(db, rack.id, name="bf-coarse", os_system="linux")
        coarse_win = _make_device(
            db, rack.id, name="bf-coarse-win", os_system="Windows"
        )
        precise = _make_device(
            db, rack.id, name="bf-precise", os_system="Ubuntu 22.04.3 LTS"
        )
        empty = _make_device(db, rack.id, name="bf-empty2", os_system=None)
        ids = [coarse.id, coarse_win.id, precise.id, empty.id]

        picked = {d.id for d in bos._load_targets(db, OPS_TARGET_TYPES, True)}
        assert {coarse.id, coarse_win.id} <= picked
        assert precise.id not in picked
        assert empty.id not in picked  # 空缺是默认模式的活
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


def test_refine_upgrades_coarse_to_precise(stub_detect, monkeypatch):
    """凭据探测拿到精确名 → 写库;粗值行被升级,精确名行不被碰。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "refine-up")
    ids: list[int] = []
    try:
        coarse = _make_device(
            db, rack.id, name="bf-centos", os_system="linux", ip_address="203.0.113.21"
        )
        precise = _make_device(
            db,
            rack.id,
            name="bf-ubuntu",
            os_system="Ubuntu 22.04.3 LTS",
            ip_address="203.0.113.22",
        )
        ids = [coarse.id, precise.id]
        _inject_targets(monkeypatch, [coarse, precise])
        stub_detect.results["203.0.113.21"] = "linux"
        stub_detect.versions["203.0.113.21"] = "CentOS Linux 7 (Core)"

        count = asyncio.run(
            bos.backfill(OPS_TARGET_TYPES, 2, dry_run=False, refine=True)
        )

        assert count == 1
        assert _reload_os(db, coarse.id) == "CentOS Linux 7 (Core)"
        assert _reload_os(db, precise.id) == "Ubuntu 22.04.3 LTS"
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()


def test_refine_skips_when_only_coarse_result(stub_detect, monkeypatch, capsys):
    """重探仍只拿到粗值(或 banner 级版本串)→ 保持原值,不算已精化。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    room, rack = _new_rack(db, "refine-skip")
    ids: list[int] = []
    try:
        still_coarse = _make_device(
            db, rack.id, name="bf-still", os_system="linux", ip_address="203.0.113.23"
        )
        banner_level = _make_device(
            db, rack.id, name="bf-banner", os_system="linux", ip_address="203.0.113.24"
        )
        ids = [still_coarse.id, banner_level.id]
        _inject_targets(monkeypatch, [still_coarse, banner_level])
        # 只拿到族类名
        stub_detect.results["203.0.113.23"] = "linux"
        # 只拿到 banner 级版本串(OpenSSH 版本不是 OS 版本)
        stub_detect.results["203.0.113.24"] = "linux"
        stub_detect.versions["203.0.113.24"] = "Linux (OpenSSH) 7.4"

        count = asyncio.run(
            bos.backfill(OPS_TARGET_TYPES, 2, dry_run=False, refine=True)
        )

        assert count == 0
        assert _reload_os(db, still_coarse.id) == "linux"
        assert _reload_os(db, banner_level.id) == "linux"
        assert "未能精确识别" in capsys.readouterr().out
    finally:
        _cleanup(db, ids, [rack.id], [room.id])
        db.close()
