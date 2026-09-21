"""报告「系统」列的 OS 名来源与单行显示。

背景：虚拟机的精确 OS 名过去拿不到（guest binding 只存粗粒度 linux/windows），
报告只能回退显示 "Windows"/"Linux"。现在巡检时把精确名留档进
``inspection_records.os_name``（迁移 0037），报告优先读冗余列；
``AgentTarget.is_windows`` 同步改成包含匹配，精确名不会再把 WinRM 误判成 SSH。
"""

from datetime import datetime, timezone

import pytest

from app.database import SessionLocal
from app.models.automation import AutomationJob, AutomationJobTarget
from app.models.inspection import InspectionRecord
from app.models.pve_connection import PveConnection
from app.models.pve_guest_binding import PveGuestBinding
from app.models.user import User
from app.services.agent import AgentTarget
from app.services.inspection_report import build_report_data
from app.services.report_pdf import build_inspection_pdf


@pytest.mark.parametrize(
    ("os_system", "expected"),
    [
        ("windows", True),
        ("Microsoft Windows Server 2022 Datacenter", True),  # QGA pretty-name
        ("Windows 11 Pro", True),
        ("linux", False),
        ("Debian GNU/Linux 12 (bookworm)", False),
        ("Ubuntu 22.04.3 LTS", False),
    ],
)
def test_agent_target_is_windows_contains_match(os_system, expected):
    target = AgentTarget(id=None, name="vm", ip_address="10.0.0.1", os_system=os_system)
    assert target.is_windows is expected


def _make_job_with_guest_record(db, os_name):
    """落库一个 PVE 虚拟机巡检任务 + 带 os_name 留档的巡检记录。

    connection_id / vmid 用时间派生的随机值:测试库里可能残留别的用例建的
    binding / role_pve_guest_access 行(共享库、按 id 快照回收),写死的 1/101
    会撞上,让别的用例 ACL 判定失真。
    """
    suffix = str(datetime.now(timezone.utc).timestamp()).replace(".", "")
    user = User(
        username=f"os_name_test_{suffix}",
        password="irrelevant",
        role="operator",
        is_active=1,
    )
    db.add(user)
    db.flush()
    job = AutomationJob(
        name="虚拟机巡检",
        job_type="inspection",
        trigger_type="manual",
        status="completed",
        risk_level="read_only",
        config_json={"mode": "core"},
        created_by=user.id,
        created_by_name=user.username,
    )
    db.add(job)
    db.flush()
    record = InspectionRecord(
        device_id=None,  # PVE 虚拟机无 devices 行(迁移 0036)
        device_name="[IT PVE] SRM-APP",
        device_ip="10.0.0.9",
        target_type="windows",
        os_name=os_name,  # 迁移 0037 留档的精确名
        mode="core",
        status="completed",
        total_items=0,
        started_at=datetime.now(timezone.utc),
        triggered_by=user.id,
    )
    db.add(record)
    db.flush()
    # 测试库连接少,避开低位 id;vmid 取时间后 6 位,基本不可能撞
    conn_id = 900000 + (int(suffix) % 10000)
    vmid = 900000 + (int(suffix) % 10000)
    target = AutomationJobTarget(
        job_id=job.id,
        device_id=None,
        device_name="[IT PVE] SRM-APP",
        device_ip="10.0.0.9",
        target_type="pve_guest",
        pve_connection_id=conn_id,
        pve_guest_type="qemu",
        pve_vmid=vmid,
        status="completed",
        result_json={"record_id": record.id},
    )
    db.add(target)
    db.commit()
    return job, record, conn_id, vmid


def test_report_uses_record_os_name_for_pve_guest():
    """虚拟机报告「系统」列:优先读 record.os_name 精确名,不再是回退的 Windows/Linux。"""
    db = SessionLocal()
    try:
        job, record, _conn_id, _vmid = _make_job_with_guest_record(
            db, "Microsoft Windows Server 2022 Datacenter"
        )
        data = build_report_data(db, job)
        assert data is not None
        host = data.hosts[0]
        assert host.os_name == "Windows Server"  # 经 short_os_name 归一化
    finally:
        db.close()


def test_report_falls_back_when_record_has_no_os_name():
    """历史记录 os_name 为 NULL 时回退旧逻辑(虚拟机 → target_type 族类名)。"""
    db = SessionLocal()
    try:
        job, record, _conn_id, _vmid = _make_job_with_guest_record(db, None)
        data = build_report_data(db, job)
        assert data is not None
        assert data.hosts[0].os_name == "Windows"
    finally:
        db.close()


def test_report_falls_back_to_binding_os_name_when_record_is_coarse():
    """历史记录留的是粗值 'linux' 时,回退到 binding.os_name 的精确名。

    这正是 Require 虚拟机的场景:记录生成时凭据精化还没上线,os_name 留了
    'linux';之后 detect-os / 巡检把 binding.os_name 精化成 'CentOS Linux 7
    (Core)',报告应显示更准的那个。
    """
    db = SessionLocal()
    conn = None
    try:
        job, record, _conn_id, vmid = _make_job_with_guest_record(db, "linux")
        # binding 外键要求真实 pve_connections 行;但 collect_pve_guest_candidates
        # 拉所有 enabled 连接,多一条真实行会让 role_pve_scope 的 candidates 断言翻倍,
        # 而 conftest 是 session 末才回收。所以这里用例自己 finally 清掉连接。
        conn = PveConnection(
            name="Test PVE",
            host="192.0.2.1",
            token_id="root@pam!t",
            token_secret_enc="x",
            enabled=0,  # 不参与 candidates 查询,双保险
        )
        db.add(conn)
        db.flush()
        job.targets[0].pve_connection_id = conn.id
        db.add(
            PveGuestBinding(
                connection_id=conn.id,
                guest_type="qemu",
                vmid=vmid,
                ip_address="10.0.0.9",
                os_system="linux",
                os_name="CentOS Linux 7 (Core)",  # 已被凭据探测精化
            )
        )
        db.commit()
        data = build_report_data(db, job)
        assert data is not None
        assert data.hosts[0].os_name == "CentOS"  # 经 short_os_name 归一化
    finally:
        if conn is not None:
            db.query(PveGuestBinding).filter_by(connection_id=conn.id).delete()
            db.query(PveConnection).filter_by(id=conn.id).delete()
            db.commit()
        db.close()


def test_report_ip_falls_back_to_record_for_pve_guest():
    """PVE 虚拟机的 target.device_ip 为 None(运行时经 QGA 才解析 IP),
    报告 IP 列应回退到巡检记录里留档的真实 IP。"""
    db = SessionLocal()
    try:
        job, record, _c, _v = _make_job_with_guest_record(db, "linux")
        # 构造真实场景:target 没带 IP,record 有
        job.targets[0].device_ip = None
        record.device_ip = "192.168.4.169"
        db.commit()
        data = build_report_data(db, job)
        assert data is not None
        assert data.hosts[0].ip == "192.168.4.169"
    finally:
        db.close()


def test_pdf_renders_os_name_on_one_line():
    """'Windows Server' 里的空格必须是非断行空格,不允许折成两行。"""
    db = SessionLocal()
    try:
        job, record, _c, _v = _make_job_with_guest_record(
            db, "Microsoft Windows Server 2022 Datacenter"
        )
        data = build_report_data(db, job)
        assert data is not None
        pdf_bytes = build_inspection_pdf(data)
        # 生成不炸且产出合法 PDF 即可(压缩流里断言不了单个空格字符)
        assert pdf_bytes.startswith(b"%PDF")
    finally:
        db.close()
