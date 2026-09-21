"""PVE(Proxmox VE)平台连接配置。

存 PVE 主机的 API Token 凭证(token_secret 加密存储,同 credentials 的加密方式)。
"""

from datetime import datetime, timezone

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class PveConnection(Base):
    """一条 PVE 平台连接(单节点/集群入口)。"""

    __tablename__ = "pve_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8006)
    # 形如 "root@pam!mytoken" 的 token id + 加密的 token secret
    token_id: Mapped[str] = mapped_column(String(255), nullable=False)
    token_secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    verify_ssl: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(timezone.utc)
    )
