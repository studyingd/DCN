"""Agent 诊断 API —— 只读排障(LLM tool-calling)。

边界:所有设备执行面都收敛到 agent_commands 注册表的只读命令,
LLM 自由文本永远不进 shell;每次诊断落 agent_runs 审计。

配置:/api/agent/config(settings:manage)读写 Agent 参数,DB 优先、.env 兜底,
API Key 加密存储、接口只回显掩码。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.rate_limiter import limiter
from app.models.agent_run import AgentRun
from app.models.device import OPS_TARGET_TYPES, Device
from app.models.user import User
from app.services.agent import get_agent_config, start_diagnosis
from app.services.agent_commands import get_diagnostics_for_device
from app.services.crypto import encrypt
from app.services.permissions import (
    get_user_device_ids,
    require_permission,
    user_can_access_device,
)
from app.services.settings import get_setting, set_setting
from app.validators import validate_outbound_url

router = APIRouter(prefix="/api/agent", tags=["agent"])


class DiagnoseRequest(BaseModel):
    device_id: int
    question: str = Field(min_length=2, max_length=500)


def _get_device_checked(device_id: int, db: Session, user: User) -> Device:
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not user_can_access_device(user, device_id, db):
        raise HTTPException(status_code=403, detail="无权访问该设备")
    return device


def _check_ready(db: Session):
    cfg = get_agent_config(db)
    if not cfg.enabled:
        raise HTTPException(
            status_code=503,
            detail="Agent 功能未启用(系统管理 → Agent 配置中开启,或设 AGENT_ENABLED=true)",
        )
    if not cfg.configured:
        raise HTTPException(
            status_code=503,
            detail="Agent 未配置 LLM(接口地址/密钥/模型),请到 系统管理 → Agent 配置 完善",
        )
    return cfg


@router.get("/status")
def agent_status(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("device:remote")),
):
    """Agent 可用性(前端据此显示配置引导)。"""
    cfg = get_agent_config(db)
    return {"enabled": cfg.enabled, "configured": cfg.configured}


@router.get("/devices")
def list_agent_devices(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:remote")),
):
    """Agent 可诊断的设备(纳管类型,RBAC 过滤)。"""
    query = (
        db.query(Device).filter(Device.type.in_(OPS_TARGET_TYPES)).order_by(Device.name)
    )
    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        query = query.filter(Device.id.in_(allowed_ids))
    return [
        {
            "id": d.id,
            "name": d.name,
            "ip_address": d.ip_address,
            "os_system": d.os_system,
            "status": d.status,
            "has_credential": d.has_credential,
        }
        for d in query.all()
    ]


@router.get("/catalog/{device_id}")
def get_catalog(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:remote")),
):
    """该设备可用的只读诊断项(供前端展示 Agent 的"工具箱")。"""
    device = _get_device_checked(device_id, db, current_user)
    catalog = get_diagnostics_for_device(device)
    return {
        "device_id": device_id,
        "os": "windows" if device.is_windows else "linux",
        "items": [{"key": k, "label": v["label"]} for k, v in catalog.items()],
    }


@router.post("/diagnose")
# 一次诊断会拉起 LLM + SSH、最多 AGENT_MAX_STEPS 步、等待上限 300s，是全平台最贵的
# 单次操作。全局默认 100/minute 对它太宽松(100 次/分钟足以打满诊断线程池和 LLM 配额)，
# 这里按用户单独收紧。
@limiter.limit("10/minute")
def diagnose(
    body: DiagnoseRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:remote")),
):
    """发起一次只读诊断——立即返回 run_id,后台线程执行,
    前端轮询 GET /runs/{id} 获取实时步骤与最终报告。"""
    cfg = _check_ready(db)
    device = _get_device_checked(body.device_id, db, current_user)
    if not device.ip_address:
        raise HTTPException(status_code=400, detail="设备未配置 IP 地址")

    run_id = start_diagnosis(device, body.question.strip(), current_user.id, cfg)
    return {"run_id": run_id, "status": "running"}


# ── Agent 参数配置(系统管理页;settings:manage)──


class AgentConfigBody(BaseModel):
    enabled: bool
    base_url: str = Field(default="", max_length=200)
    model: str = Field(default="", max_length=100)
    api_key: str = Field(default="", max_length=200)  # 留空 = 保持不变
    max_steps: int = Field(default=8, ge=1, le=20)


def _config_view(db: Session) -> dict:
    cfg = get_agent_config(db)
    api_key_set = bool(get_setting(db, "agent_api_key") or cfg.api_key)
    return {
        "enabled": cfg.enabled,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "max_steps": cfg.max_steps,
        "api_key_set": api_key_set,
        # 只回显末 4 位,完整密钥永不出库
        "api_key_preview": f"****{cfg.api_key[-4:]}" if cfg.api_key else "",
    }


@router.get("/config")
def get_agent_config_view(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    return _config_view(db)


@router.put("/config")
def update_agent_config(
    body: AgentConfigBody,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    set_setting(db, "agent_enabled", "true" if body.enabled else "false")
    set_setting(db, "agent_base_url", body.base_url.strip())
    set_setting(db, "agent_model", body.model.strip())
    set_setting(db, "agent_max_steps", str(body.max_steps))
    if body.api_key.strip():  # 留空保持原密钥
        set_setting(db, "agent_api_key", encrypt(body.api_key.strip()))
    return _config_view(db)


@router.post("/config/test")
def test_agent_config(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    """用当前生效配置发一个最小请求,验证 LLM 接口可达。"""
    import requests as req

    from app.services.agent import AgentError

    cfg = get_agent_config(db)
    if not cfg.configured:
        raise HTTPException(status_code=400, detail="配置不完整(接口地址/密钥/模型)")
    try:
        validate_outbound_url(cfg.base_url)
        resp = req.post(
            f"{cfg.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json={
                "model": cfg.model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
            timeout=20,
            allow_redirects=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except req.RequestException as exc:
        raise AgentError(f"连接失败: {exc}") from exc
    if resp.status_code != 200:
        raise AgentError(f"接口返回 {resp.status_code}: {resp.text[:150]}")
    return {"ok": True, "message": "连接成功,模型响应正常"}


class ModelListRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", max_length=200)  # 留空则使用已保存的密钥


@router.post("/config/models")
def list_llm_models(
    body: ModelListRequest,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_permission("settings:manage")),
):
    """拉取 OpenAI 兼容接口的 /models 列表,供模型名称下拉选择。"""
    import requests as req

    from app.services.agent import AgentError

    base_url = body.base_url.strip().rstrip("/")
    try:
        validate_outbound_url(base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    api_key = body.api_key.strip()
    if not api_key:
        api_key = get_agent_config(db).api_key  # 用已保存的密钥兜底
    if not api_key:
        raise HTTPException(status_code=400, detail="请填写 API Key(或先保存配置)")

    try:
        resp = req.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
            allow_redirects=False,
        )
    except req.RequestException as exc:
        raise AgentError(f"连接失败: {exc}") from exc
    if resp.status_code != 200:
        raise AgentError(f"接口返回 {resp.status_code}: {resp.text[:150]}")

    try:
        data = resp.json()
        ids = sorted({m.get("id") for m in data.get("data", []) if m.get("id")})
    except ValueError as exc:
        raise AgentError("接口响应不是合法的 /models JSON") from exc
    if not ids:
        raise AgentError("接口未返回任何模型,请手动输入模型名称")
    return {"models": ids}


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:remote")),
):
    """单次诊断详情(含实时步骤)——诊断进行中前端轮询此接口。"""
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="诊断记录不存在")
    # 仅发起人或对该设备有权限者可查看
    if run.user_id != current_user.id and not user_can_access_device(
        current_user, run.device_id, db
    ):
        raise HTTPException(status_code=403, detail="无权查看该诊断记录")

    import json as _json

    steps = []
    if run.steps_json:
        try:
            steps = _json.loads(run.steps_json)
        except ValueError:
            steps = []

    return {
        "id": run.id,
        "device_id": run.device_id,
        "device_name": run.device_name,
        "device_ip": run.device_ip,
        "question": run.question,
        "status": run.status,
        "report": run.report,
        "error": run.error,
        "steps": steps,
        "duration_ms": run.duration_ms,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/runs")
def list_runs(
    device_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("device:remote")),
):
    """诊断历史(审计),按 RBAC 设备范围过滤。"""
    query = db.query(AgentRun).order_by(AgentRun.created_at.desc())

    allowed_ids = get_user_device_ids(current_user, db)
    if allowed_ids is not None:
        # PVE guest Agent runs intentionally have no ``devices`` FK.  Keep
        # those records visible to their initiator while still applying the
        # normal device-scope filter to regular device runs.
        query = query.filter(
            or_(
                AgentRun.device_id.in_(allowed_ids),
                (AgentRun.device_id.is_(None) & (AgentRun.user_id == current_user.id)),
            )
        )
    if device_id:
        query = query.filter(AgentRun.device_id == device_id)

    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "device_id": r.device_id,
                "device_name": r.device_name,
                "device_ip": r.device_ip,
                "question": r.question,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "report": r.report,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
