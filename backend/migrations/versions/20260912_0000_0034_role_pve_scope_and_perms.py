"""角色权限模型收敛 + 虚拟机 ACL。

A. ``role_pve_guest_access``：``role_device_access`` 只能指向 ``devices`` 行，
   虚拟机（``pve_guest_bindings`` 里的 connection_id + guest_type + vmid）因此
   完全落在角色授权之外，PVE 访问只能是全局开关。新表按稳定身份记录被授权的
   guest，与 ``business_pve_guests`` 同构。

B. ``roles.pve_scope``：与 ``device_scope`` 独立，默认 ``all``，既有角色行为不变
   （此前指定设备范围并不会限制虚拟机）。

C. ``roles.permissions`` 目录收敛：12 项 → 8 项，旧键按下表就地改写，
   ``script/inspection/alert:manage`` 合并为 ``automation:manage``，
   ``agent:use`` 并入 ``device:remote``，``docker:control`` 拆给
   ``device:manage`` + ``pve:manage``（容器控制端点同时服务普通设备与 guest）。
   同时移除 ``settings:manage`` 的 PVE 后门语义，改为给持有该键的角色补授
   ``pve:manage``。

全部步骤幂等：表/列已存在则跳过，权限改写只替换已知旧键。
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0034_role_pve_scope_and_perms"
down_revision: Union[str, None] = "0033_drop_inspection_vendor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACL_TABLE = "role_pve_guest_access"
ACL_INDEX = "ix_role_pve_guest_access_role"

# 旧权限键 → 新权限键（一对多按顺序展开）
PERMISSION_RENAME: dict[str, tuple[str, ...]] = {
    "script:manage": ("automation:manage",),
    "inspection:manage": ("automation:manage",),
    "alert:manage": ("automation:manage",),
    "agent:use": ("device:remote",),
    "docker:control": ("device:manage", "pve:manage"),
    # 更早期的历史键，一并归位
    "device:read": ("device:view",),
    "room:read": ("device:view",),
    "device:write": ("device:manage",),
    "device:delete": ("device:manage",),
    "room:write": ("device:manage",),
    "device:terminal": ("device:remote",),
    # settings:manage 曾兼任 PVE 管理后门；后门移除后显式补上 pve:manage，
    # 避免依赖该行为的既有角色突然失去虚拟化入口。
    "settings:manage": ("settings:manage", "pve:manage"),
}


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "roles" in inspector.get_table_names():
        if "pve_scope" not in _columns(bind, "roles"):
            op.add_column(
                "roles",
                sa.Column(
                    "pve_scope",
                    sa.String(length=16),
                    nullable=False,
                    server_default="all",
                ),
            )
        _rewrite_permissions(bind)

    if ACL_TABLE not in inspector.get_table_names():
        op.create_table(
            ACL_TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("role_id", sa.Integer(), nullable=False),
            sa.Column("connection_id", sa.Integer(), nullable=False),
            sa.Column("guest_type", sa.String(length=8), nullable=False),
            sa.Column("vmid", sa.Integer(), nullable=False),
            sa.Column("guest_name", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["connection_id"], ["pve_connections.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "role_id",
                "connection_id",
                "guest_type",
                "vmid",
                name="uq_role_pve_guest",
            ),
        )
        op.create_index(ACL_INDEX, ACL_TABLE, ["role_id"])


def _rewrite_permissions(bind) -> None:
    """把角色里已下线的权限键改写为合并后的新键（去重、保持原顺序）。"""
    roles = sa.table(
        "roles", sa.column("id", sa.Integer), sa.column("permissions", sa.Text)
    )
    for role_id, raw in bind.execute(sa.select(roles.c.id, roles.c.permissions)):
        try:
            perms = json.loads(raw or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(perms, list):
            continue
        migrated: list[str] = []
        for key in perms:
            for new_key in PERMISSION_RENAME.get(key, (key,)):
                if new_key not in migrated:
                    migrated.append(new_key)
        if migrated != perms:
            bind.execute(
                sa.update(roles)
                .where(roles.c.id == role_id)
                .values(permissions=json.dumps(migrated))
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if ACL_TABLE in inspector.get_table_names():
        if ACL_INDEX in {i["name"] for i in inspector.get_indexes(ACL_TABLE)}:
            op.drop_index(ACL_INDEX, table_name=ACL_TABLE)
        op.drop_table(ACL_TABLE)

    if "roles" in inspector.get_table_names():
        if "pve_scope" in _columns(bind, "roles"):
            op.drop_column("roles", "pve_scope")
        # 权限键合并是有损的（automation:manage 无法还原成三个旧键），
        # 回滚只把新键映射回一个代表性旧键，保证角色不至于完全失权。
        rollback = {
            "automation:manage": "script:manage",
        }
        roles = sa.table(
            "roles", sa.column("id", sa.Integer), sa.column("permissions", sa.Text)
        )
        for role_id, raw in bind.execute(sa.select(roles.c.id, roles.c.permissions)):
            try:
                perms = json.loads(raw or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(perms, list):
                continue
            migrated = [rollback.get(p, p) for p in perms]
            if migrated != perms:
                bind.execute(
                    sa.update(roles)
                    .where(roles.c.id == role_id)
                    .values(permissions=json.dumps(migrated))
                )
