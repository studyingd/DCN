"""回填/精化 ``devices.os_system``。

背景:设备表单早期把「支持 Windows 远程」写死成 ``['server', 'host']``,云服务器
因此跳过了操作系统自动探测(``DeviceForm.vue`` 的 ``supportsWindowsRemote``),
``os_system`` 留空。而 ``os_system`` 决定远程通道选择:

  * ``services/monitor.py`` —— Windows 主探 WinRM 5985,否则探 SSH 22;
  * ``services/inspection_commands.py`` ``get_target_type()`` —— Windows 走 WinRM,
    其余默认 linux 走 SSH;
  * ``services/metrics_collector.py`` —— Linux / Windows 采集命令完全不同。

``os_system`` 为空时这些逻辑一律按 Linux+SSH 处理,Windows 云服务器的在线状态、
巡检和批量脚本会全部执行失败。前端已放开探测,本脚本负责补齐存量数据。

``--refine``(精化)解决的是另一个问题:banner 识别拿不到发行版版本号
(如 CentOS 原版 'SSH-2.0-OpenSSH_7.4' 不带发行版注释),台账里只落了个
粗粒度的 'linux'/'windows'。精化模式用台账凭据重探(SSH 读 /etc/os-release、
WinRM 读 Win32_OperatingSystem.Caption),把粗值升级成 'CentOS Linux 7 (Core)'
级别的精确名——PDF 报告「系统」列和机房管理列表显示的都是这个值。

用法::

    cd backend
    .venv/bin/python backfill_os_system.py --dry-run          # 只探测,不写库
    .venv/bin/python backfill_os_system.py --refine --dry-run # 看精化候选
    .venv/bin/python backfill_os_system.py --refine --yes     # 精化并写库
    .venv/bin/python backfill_os_system.py --type cloud_server --yes
    .venv/bin/python backfill_os_system.py --concurrency 4 --yes

脚本会解密并使用设备已保存的远程凭据以提高识别精度(与 ``POST
/api/devices/os-detect`` 的行为一致);无凭据时仅凭端口指纹与 SSH banner 判断。

只更新缺失或粗粒度的行,不会覆盖已有的精确值,因此可以安全重跑。
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path,与 init_db.py 保持一致。
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 先从项目根 .env 加载环境变量(与 app.config 行为一致)。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # python-dotenv 未安装时,依赖外部已设置的环境变量

if not os.getenv("DATABASE_URL"):
    sys.exit("缺少 DATABASE_URL:请在项目根 .env 中配置,或显式导出该环境变量后重试。")

# 以下导入必须在 DATABASE_URL 就绪之后,app.config 在导入时读取环境变量。
from app.database import SessionLocal  # noqa: E402
from app.models.device import OPS_TARGET_TYPES, Device  # noqa: E402
from app.services.device_credentials import resolve_credentials  # noqa: E402
from app.services.os_detect import detect_os  # noqa: E402

# 粗粒度族类值:banner 只能识别到这一层,值得用凭据重探精确版本。
_COARSE_VALUES = {"linux", "windows", "unknown"}

# banner 级的版本串(如 'Linux (OpenSSH) 7.4' / 'Linux (Dropbear)')不算精确名——
# 那是 SSH 软件的版本,不是 OS 版本,写进台账一样无法显示发行版。
_BANNER_LEVEL_MARKERS = ("(openssh)", "(dropbear)")


def _is_refine_candidate(os_system: str | None) -> bool:
    return (os_system or "").strip().lower() in _COARSE_VALUES


def _load_targets(db, types: tuple[str, ...], refine: bool) -> list[Device]:
    """挑出需要回填/精化 os_system 的纳管设备。"""
    query = db.query(Device).filter(Device.type.in_(types))
    rows = query.order_by(Device.id).all()
    if refine:
        return [d for d in rows if _is_refine_candidate(d.os_system)]
    return [d for d in rows if not (d.os_system or "").strip()]


async def _probe(device: Device) -> str:
    """返回要写入台账的值:优先精确版本名,拿不到再退回族类名。"""
    username, password, _ssh_key = resolve_credentials(device)
    result = await detect_os(
        device.ip_address or "",
        username=username or None,
        password=password or None,
        ssh_port=device.ssh_port or 22,
        winrm_port=device.winrm_port or None,
    )
    version = (result.os_version or "").strip()
    family = (result.os_system or "").strip()
    if version and not any(m in version.lower() for m in _BANNER_LEVEL_MARKERS):
        return version
    return family


async def backfill(
    types: tuple[str, ...],
    concurrency: int,
    dry_run: bool,
    refine: bool = False,
) -> int:
    db = SessionLocal()
    try:
        targets = _load_targets(db, types, refine)
        if not targets:
            what = "os_system 为粗粒度值" if refine else "os_system 缺失"
            print(f"没有{what}的设备,无需处理。")
            return 0

        print(
            f"待探测 {len(targets)} 台设备(类型 {', '.join(types)},"
            f"并发 {concurrency}{'，dry-run' if dry_run else ''}):"
        )
        for device in targets:
            print(
                f"  #{device.id:<4} {device.name:<24} "
                f"{device.ip_address or '无 IP':<16} {device.type}"
            )

        # 快照先于探测读取:凭据解密后的明文不落盘、不进日志,只用完即弃。
        snapshot = [
            (device.id, device.name, device.ip_address, device.type, device)
            for device in targets
        ]

        sem = asyncio.Semaphore(concurrency)

        async def probe_one(entry):
            device_id, name, ip, dtype, device = entry
            if not ip:
                return device_id, name, dtype, "", "跳过:未配置 IP"
            async with sem:
                try:
                    detected = await _probe(device)
                except Exception as exc:  # 单台失败不影响其余设备
                    return device_id, name, dtype, "", f"探测异常: {exc}"
            if not detected or detected == "unknown":
                return device_id, name, dtype, detected, "未识别"
            return device_id, name, dtype, detected, ""

        results = await asyncio.gather(*(probe_one(e) for e in snapshot))

        detected_count = 0
        for device_id, name, dtype, detected, note in results:
            if note:
                print(f"  #{device_id:<4} {name:<24} {dtype:<12} {note}")
                continue
            if refine and _is_refine_candidate(detected):
                # 重探也只拿到粗粒度值(凭据不可用/仍是 banner 级),写了也没意义。
                print(f"  #{device_id:<4} {name:<24} {dtype:<12} 未能精确识别,保持原值")
                continue
            detected_count += 1
            action = "将写入" if dry_run else "已写入"
            print(f"  #{device_id:<4} {name:<24} {dtype:<12} {action} {detected}")
            if not dry_run:
                device = db.query(Device).filter(Device.id == device_id).first()
                # 复查一次:避免覆盖探测期间用户在界面上手工填写的值。
                if device:
                    current = device.os_system
                    if refine:
                        if _is_refine_candidate(current):
                            device.os_system = detected
                    elif not (current or "").strip():
                        device.os_system = detected

        if not dry_run:
            db.commit()
            verb = "已精化" if refine else "已回填"
            print(f"{verb} {detected_count} 台设备的 os_system。")
        else:
            verb = "可精化" if refine else "可回填"
            print(f"[dry-run] {verb} {detected_count} 台设备,未写库。")
        return detected_count
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="回填缺失的 devices.os_system(云服务器历史上被跳过探测)"
    )
    parser.add_argument(
        "--type",
        action="append",
        choices=list(OPS_TARGET_TYPES),
        help="只处理指定设备类型,可重复;默认全部纳管类型",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="并发探测数(默认 2;探测含端口超时,过高会拖慢且易误判)",
    )
    parser.add_argument(
        "--refine",
        action="store_true",
        help="精化模式:处理 os_system 为粗粒度值(linux/windows/unknown)的设备,"
        "用凭据重探并升级成精确版本名(如 'CentOS Linux 7 (Core)')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只探测并打印结果,不写库",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过确认提示",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    types = tuple(args.type) if args.type else OPS_TARGET_TYPES
    if args.concurrency < 1:
        parser.error("--concurrency 必须 >= 1")

    if not args.dry_run and not args.yes:
        answer = input(
            f"将探测并回填 os_system(类型: {', '.join(types)})。继续? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            print("已取消。")
            return

    asyncio.run(backfill(types, args.concurrency, args.dry_run, refine=args.refine))


if __name__ == "__main__":
    main()
