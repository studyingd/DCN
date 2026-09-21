"""巡检项 item_type 的标签契约 —— 把前后端命名空间漂移钉在测试里。

`inspection_item_results.item_type` 存的是 `inspection_commands.py` 里两张命令注册表
(LINUX/WINDOWS_COMMANDS)的键，前端要靠一张 item_type → 中文名的表来渲染。
这张表曾经在前端存在三份:两个组件各一份(键集正确)，外加
`frontend/src/utils/inspectionLabels.ts` 一份**凭空虚构**的 27 键
(`cpu_usage`/`bgp_status`/`nat_rules`/`vpn_status`…)——后端从未产出过这些 item_type，
而那份文件自称 "Single source of truth" 却没有任何生产代码导入它，谁按名字去用谁就
会看到满屏原始英文键。

现在统一到 `frontend/src/utils/inspectionLabels.ts`(键集与后端逐一对齐)，两个组件改为
导入。这里同时钉住四件事:
  1. 后端自身一致 —— 注册表里每个 item_type 都有标签，QUICK_ITEMS 不引用不存在的项;
  2. 键集冻结 —— 只剩 linux/windows 两类主机巡检项，共 18 个 item_type;
  3. 前后端键集一致 —— 后端加巡检项时忘了给前端加标签会直接红;
  4. 不再回退到组件内重复定义。
"""

import re
from pathlib import Path

import pytest

from app.services.inspection_commands import (
    ITEM_LABELS,
    LINUX_COMMANDS,
    QUICK_ITEMS,
    WINDOWS_COMMANDS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_LABELS = REPO_ROOT / "frontend" / "src" / "utils" / "inspectionLabels.ts"

# 主机巡检下线了网络设备那一半之后，item_type 命名空间冻结为
# 「LINUX_COMMANDS 13 键 ∪ WINDOWS_COMMANDS 13 键」= 18 键。
# 注意 firewall / network 是主机自身的防火墙状态与网卡配置，与被移除的
# 网络设备类型同名不同义，必须留着。
EXPECTED_ITEM_TYPES = {
    "cpu",
    "memory",
    "disk",
    "load",
    "network",
    "ports",
    "processes",
    "os_version",
    "logs",
    "failed_services",
    "firewall",
    "security_updates",
    "logins",
    "system_info",
    "services",
    "event_logs",
    "updates",
    "uptime",
}


def _command_item_types() -> set[str]:
    """两张命令注册表里全部可用的 item_type(顶层键就是 item_type)。"""
    return set(LINUX_COMMANDS) | set(WINDOWS_COMMANDS)


def _frontend_labels() -> dict[str, str]:
    """从 TS 源码里抠出 ITEM_LABELS 字面量(键集才是契约,文案允许与后端不同)。"""
    if not FRONTEND_LABELS.exists():
        pytest.skip(f"前端源码不在预期路径: {FRONTEND_LABELS}")
    src = FRONTEND_LABELS.read_text(encoding="utf-8")
    block = re.search(r"export const ITEM_LABELS[^=]*=\s*\{(.*?)\n\}", src, re.S)
    assert block, f"{FRONTEND_LABELS.name} 里找不到 export const ITEM_LABELS 字面量"
    return dict(re.findall(r"^\s+([A-Za-z_][\w]*):\s*'([^']*)'", block.group(1), re.M))


# ── 1. 后端自身一致 ────────────────────────────────────────────────────────


def test_every_command_item_type_has_a_label():
    """注册表里能被执行的 item_type 都必须有中文名,否则 UI 直接显示原始键。"""
    missing = sorted(_command_item_types() - set(ITEM_LABELS))
    assert not missing, f"这些 item_type 在 ITEM_LABELS 里没有标签: {missing}"


def test_no_orphan_labels():
    """反过来:有标签却没有任何命令的键是历史残留,会误导后续维护。"""
    orphans = sorted(set(ITEM_LABELS) - _command_item_types())
    assert not orphans, f"这些标签没有对应的巡检命令: {orphans}"


def test_quick_items_are_real_item_types():
    """QUICK_ITEMS 引用的项必须在对应注册表里存在,否则快速巡检会静默少查几项。"""
    registries = {"linux": LINUX_COMMANDS, "windows": WINDOWS_COMMANDS}
    for target_type, items in QUICK_ITEMS.items():
        available = set(registries[target_type])
        unknown = sorted(set(items) - available)
        assert not unknown, f"{target_type} 的快速巡检项不存在: {unknown}"


def test_labels_are_non_empty():
    assert all(v.strip() for v in ITEM_LABELS.values()), "存在空白标签"


# ── 2. 键集冻结 ────────────────────────────────────────────────────────────


def test_item_type_namespace_is_frozen():
    """命令注册表与标签表的键集必须恰好是约定的 18 项。

    网络设备巡检项(interface/version/routes/log/environment/power/fan/stp/
    vlan/arp/mac)已随子系统整体下线;再往回加、或者偷偷改名,都会在这里红掉——
    前端那张标签表是按同一份键集写死的。
    """
    commands = _command_item_types()
    assert commands == EXPECTED_ITEM_TYPES, (
        f"命令注册表键集漂移 —— 多出: {sorted(commands - EXPECTED_ITEM_TYPES)}; "
        f"缺少: {sorted(EXPECTED_ITEM_TYPES - commands)}"
    )
    labels = set(ITEM_LABELS)
    assert labels == EXPECTED_ITEM_TYPES, (
        f"ITEM_LABELS 键集漂移 —— 多出: {sorted(labels - EXPECTED_ITEM_TYPES)}; "
        f"缺少: {sorted(EXPECTED_ITEM_TYPES - labels)}"
    )


def test_quick_items_only_cover_host_target_types():
    """快速巡检只剩 linux/windows 两类主机,不再为网络设备保留条目。"""
    assert set(QUICK_ITEMS) == {"linux", "windows"}, (
        f"QUICK_ITEMS 的 target_type 应为 linux/windows，实际: {sorted(QUICK_ITEMS)}"
    )


# ── 3. 前后端键集一致 ──────────────────────────────────────────────────────


def test_frontend_label_keys_match_backend():
    """前端那张表的键集必须与后端 item_type 完全一致。

    文案刻意不做比对:后端 ITEM_LABELS 用于自定义巡检的勾选目录,前端那份要挤进
    表格列宽(如 failed_services 后端叫「失败服务」、前端可能叫「服务」),两者本就
    允许不同。
    """
    frontend = _frontend_labels()
    backend = set(ITEM_LABELS)
    assert set(frontend) == backend, (
        f"前后端 item_type 键集不一致 —— 后端有前端无: {sorted(backend - set(frontend))}; "
        f"前端有后端无: {sorted(set(frontend) - backend)}"
    )


def test_frontend_labels_are_non_empty():
    empty = sorted(k for k, v in _frontend_labels().items() if not v.strip())
    assert not empty, f"前端存在空白标签: {empty}"


# ── 4. 不再回退到组件内重复定义 ────────────────────────────────────────────


def test_no_duplicate_label_maps_in_components():
    """标签表只允许存在于 utils/inspectionLabels.ts 一处。

    曾经 InspectionPanel.vue 与 InspectionResultCard.vue 各自维护一份 29 键的副本，
    加一个巡检项要改三个地方，漏一个就渲染成英文键。
    """
    src_dir = REPO_ROOT / "frontend" / "src"
    if not src_dir.exists():
        pytest.skip(f"前端源码不在预期路径: {src_dir}")
    offenders = []
    for path in src_dir.rglob("*.vue"):
        if re.search(r"\bITEM_LABELS\s*[:=]", path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"这些组件又自己定义了 ITEM_LABELS: {offenders}"
