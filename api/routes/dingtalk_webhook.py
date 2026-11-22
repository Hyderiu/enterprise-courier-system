"""
钉钉集成路由 —— 模块 2 + 模块 3

模块 2: OA 审批动态数据源
  - 钉钉连接平台配置连接器时会调这些接口，返回部门/项目下拉选项
  - 员工打开 OA 寄件表单时自动拉取最新项目/部门列表

模块 3: 审批事件订阅
  - 钉钉企业内部应用 → 事件订阅 → 配置回调地址
  - 审批通过后钉钉推 bpms_instance_change 事件
  - 我们系统收到 → 拉审批详情 → 创建订单 → 下单

⚠️  钉钉事件推送要求回调地址公网可达 + HTTPS
    本地开发可用 ngrok/cpolar 等内网穿透工具
"""

import hashlib
import hmac
import base64
import json
import logging
import re
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_

from database.connection import get_db
from database import models
from config.settings import settings

log = logging.getLogger("dingtalk.webhook")

router = APIRouter()


# ============================================================
# 钉钉事件回调加解密（官方标准实现）
# 参考: https://open.dingtalk.com/document/orgapp/verified-event-callback-mode
# ============================================================

class DingSignError(Exception):
    pass


def _verify_signature(timestamp: str, nonce: str, body: str, signature: str) -> bool:
    """
    钉钉事件推送签名验证
    排序后拼接 token + timestamp + nonce + body，做 SHA256 HMAC
    """
    token = settings.dingtalk_event_token
    if not token:
        log.error("[验签] dingtalk_event_token 未配置，拒绝回调")
        return False
    sort_str = "".join(sorted([token, str(timestamp), str(nonce), body]))
    expected = hmac.new(
        token.encode("utf-8"), sort_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _timestamp_is_fresh(timestamp: str, tolerance_seconds: int = 300) -> bool:
    try:
        value = float(timestamp)
        if value > 10_000_000_000:  # 兼容毫秒时间戳
            value /= 1000
        return abs(time.time() - value) <= tolerance_seconds
    except (TypeError, ValueError):
        return False


def _decrypt_aes(encrypt_str: str) -> dict:
    """
    解密钉钉 AES 加密的消息体
    钉钉消息格式: 20字节随机串 + 4字节corpId长度(big-endian) + corpId + json明文
    """
    if not settings.dingtalk_event_aes_key:
        # 没配 EncodingAESKey 说明开的不是加密推送，直接用原始 payload
        try:
            return json.loads(encrypt_str)
        except Exception:
            return {}
    try:
        from Crypto.Cipher import AES  # pip install pycryptodome
    except ImportError:
        log.warning("[AES] pycryptodome 未安装，跳过解密")
        try:
            return json.loads(encrypt_str)
        except Exception:
            return {}

    aes_key = base64.b64decode(settings.dingtalk_event_aes_key + "=")  # 补 padding
    iv = aes_key[:16]
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    raw = cipher.decrypt(base64.b64decode(encrypt_str))

    # 去 PKCS7 padding
    pad_len = raw[-1]
    if 1 <= pad_len <= 16:
        raw = raw[:-pad_len]

    # 解析结构
    if len(raw) < 24:
        return {}
    id_len = int.from_bytes(raw[16:20], "big")
    corp_id = raw[20:20 + id_len].decode("utf-8", errors="replace")
    json_str = raw[20 + id_len:].decode("utf-8", errors="replace")

    # corpId 校验（可选）
    if settings.dingtalk_corp_id and corp_id != settings.dingtalk_corp_id:
        log.warning(f"[AES] corpId 不匹配: 推送={corp_id} 预期={settings.dingtalk_corp_id}")

    return json.loads(json_str)


# ============================================================
# 模块 2: OA 审批动态数据源接口
# 钉钉连接平台连接器配置时，调这些接口拿下拉选项
# 返回格式固定为 {"options": [{"key": "唯一ID", "value": "显示名"}, ...]}
# 钉钉连接器会把 options[].key → "客户列表.选项列表.ID列表"
#                  options[].value → "客户列表.选项列表.值列表"
# ============================================================

@router.get("/projects")
def dingtalk_datasource_projects(
    dept_code: Optional[str] = Query(None, description="按部门过滤项目"),
    db: Session = Depends(get_db),
):
    """
    OA 表单 "项目" 下拉数据源
    - 表单加载时: 无 dept_code，返回全部启用的项目
    - 部门变化联动时: 带 dept_code，返回该部门下的项目
    """
    q = db.query(models.Project).filter(models.Project.enabled == True)
    if dept_code:
        q = q.filter(models.Project.dept_code == dept_code)
    items = q.order_by(models.Project.dept_code, models.Project.project_name).limit(500).all()
    options = [
        {"key": p.project_code, "value": f"{p.project_name}（{p.project_code}）"}
        for p in items
    ]
    return {"ok": True, "options": options, "total": len(options)}


@router.get("/departments")
def dingtalk_datasource_departments(db: Session = Depends(get_db)):
    """OA 表单 "部门" 下拉数据源"""
    items = db.query(models.Department).filter(models.Department.enabled == True).order_by(
        models.Department.dept_name
    ).all()
    options = [{"key": d.dept_code, "value": d.dept_name} for d in items]
    return {"ok": True, "options": options, "total": len(options)}


@router.get("/dept-projects")
def dingtalk_datasource_dept_projects(
    dept_code: str = Query(..., description="部门编号"),
    db: Session = Depends(get_db),
):
    """
    数据联动: 选了部门后自动刷新 "项目" 下拉
    钉钉连接器"控件值发生变化时"绑定到此接口
    """
    items = db.query(models.Project).filter(
        models.Project.enabled == True,
        models.Project.dept_code == dept_code,
    ).order_by(models.Project.project_name).limit(500).all()
    options = [
        {"key": p.project_code, "value": f"{p.project_name}（{p.project_code}）"}
        for p in items
    ]
    return {"ok": True, "options": options, "total": len(options)}


# ============================================================
# 模块 3: 正式事件订阅回调
# 钉钉企业内部应用 → 事件订阅 → 填公网地址 http(s)://xxx/api/dingtalk/event
# ============================================================

@router.post("/event")
async def dingtalk_event_subscription(request: Request):
    """
    钉钉审批事件回调 —— 所有事件都推到这里

    处理流程:
    1. 首次配置验证（echostr 回传）→ 直接返回
    2. 签名验证 + AES 解密
    3. 只处理: processCode 在白名单 + type=finish + result=agree
    4. 调用 dingtalk.oa.create_order_from_oa() 完成下单

    注意: 钉钉回调要求 3 秒内返回 "success"，所以处理逻辑异步化
    """
    # --- 3秒快速响应 ---
    try:
        body_text = (await request.body()).decode("utf-8")
        params = dict(request.query_params)
    except Exception:
        return {"msg": "fail"}

    # --- 验签 ---
    if not settings.dingtalk_event_token:
        raise HTTPException(503, "钉钉事件回调验签密钥未配置")
    signature = params.get("signature") or params.get("msg_signature") or ""
    timestamp = params.get("timestamp", "")
    nonce = params.get("nonce", "")
    if not signature or not timestamp or not nonce:
        raise HTTPException(401, "钉钉事件回调缺少验签参数")
    if not _timestamp_is_fresh(timestamp):
        raise HTTPException(401, "钉钉事件回调时间戳已过期")
    if not _verify_signature(timestamp, nonce, body_text, signature):
        log.error("[事件回调] 签名验证失败 timestamp=%s", timestamp)
        raise HTTPException(401, "钉钉事件回调验签失败")

    # 首次配置验证也必须先通过验签，避免公开端点被当作回显服务。
    if "echostr" in params:
        return params["echostr"]
    try:
        body_json = json.loads(body_text)
        if "echostr" in body_json:
            return body_json["echostr"]
    except Exception:
        pass

    # --- 解密消息 ---
    try:
        payload = json.loads(body_text)
        encrypt = payload.get("encrypt") or payload.get("Encrypt")
        if encrypt and not settings.dingtalk_event_aes_key:
            raise HTTPException(503, "钉钉事件回调 AES 密钥未配置")
        event = _decrypt_aes(encrypt) if encrypt else payload
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"[事件回调] 解密失败: {e}, body={body_text[:200]}")
        return {"msg": "success"}  # 解密失败也返回 success 避免钉钉反复重试

    log.info(f"[事件回调] 收到事件 payload_keys={list(event.keys())}")

    # --- 幂等: 同一个 eventId 不处理第二次 ---
    event_id = event.get("eventId") or event.get("EventId") or event.get("event_id")
    if event_id:
        db = next(get_db())
        try:
            dup = db.query(models.Order).filter(
                models.Order.approve_remark.contains(f"event_id={event_id}")
            ).first()
            if dup:
                log.info(f"[幂等] event_id={event_id} 已处理过 → {dup.order_no}")
                return {"msg": "success"}
        finally:
            db.close()

    # --- 路由: 只处理审批事件 ---
    event_type = event.get("EventType") or event.get("eventType") or event.get("type")
    process_code = event.get("processCode") or event.get("ProcessCode")
    instance_id = (event.get("processInstanceId")
                   or event.get("ProcessInstanceId")
                   or event.get("instanceCode")
                   or event.get("instance_id"))
    change_type = event.get("type") or event.get("Type")         # start / finish / terminate
    result = event.get("result") or event.get("Result")           # agree / refuse
    status = event.get("status") or event.get("Status")

    # 兼容不同钉钉版本的字段名
    process_code = process_code or event.get("ProcessDefinitionKey") or event.get("process_definition_key")
    instance_id = instance_id or event.get("ProcessInstanceId")

    log.info(f"[事件] type={event_type} processCode={process_code} "
             f"change={change_type} result={result} status={status} instance={instance_id}")

    # 过滤: 只关心寄件审批模板 + 审批完成 + 审批通过
    whitelist = [c.strip() for c in settings.dingtalk_oa_process_code.split(",") if c.strip()]
    event_is_bpms = event_type in ("bpms_instance_change", "BPMS_INSTANCE_CHANGE")
    is_finish = change_type in ("finish", "Finish") or status in ("finish", "completed")
    is_pass = result in ("agree", "Agree") or status in ("passed", "approved")

    if event_is_bpms and instance_id and is_finish and is_pass:
        # 白名单未配置时不允许任何审批模板触发生产下单。
        if not whitelist:
            log.error("[事件] DINGTALK_OA_PROCESS_CODE 未配置，拒绝触发自动下单")
            return {"msg": "success"}
        if not process_code or process_code not in whitelist:
            log.info(f"[事件] processCode={process_code} 不在白名单，跳过")
            return {"msg": "success"}

        # 异步处理（钉钉要求 3 秒内返回）
        import asyncio
        from dingtalk.oa import create_order_from_oa

        def _do_process():
            try:
                create_order_from_oa(
                    process_instance_id=instance_id,
                    event_id=event_id,
                    dry_run=False,
                )
            except Exception as exc:
                log.error(f"[事件] 处理异常 instance={instance_id}: {exc}", exc_info=True)

        # 在后台线程执行，立即返回给钉钉
        import threading
        threading.Thread(target=_do_process, daemon=True).start()

    return {"msg": "success"}


# ============================================================
# 健康检查 / 手动触发（开发调试用）
# ============================================================

@router.get("/health")
def dingtalk_health():
    """钉钉连接健康检查"""
    return {
        "ok": True,
        "msg": "DingTalk integration ready",
        "configured": {
            "app_key": bool(settings.dingtalk_app_key),
            "agent_id": bool(settings.dingtalk_agent_id),
            "corp_id": bool(settings.dingtalk_corp_id),
            "public_base_url": settings.public_base_url or "(未配置)",
            "approval_notification_ready": bool(
                settings.dingtalk_app_key
                and settings.dingtalk_app_secret
                and settings.dingtalk_agent_id
                and settings.public_base_url
            ),
            "event_token": bool(settings.dingtalk_event_token),
            "event_aes_key": bool(settings.dingtalk_event_aes_key),
            "process_code": settings.dingtalk_oa_process_code or "(未配置)",
            "crm_base": settings.crm_base_url or "(未配置)",
        },
    }


@router.post("/manual-trigger")
async def dingtalk_manual_trigger(body: dict):
    """
    手动触发 OA→快递 下单流程（开发测试用）
    传 process_instance_id 即可，会跳过事件推送直接走完整下单流程
    """
    process_instance_id = body.get("process_instance_id") or body.get("instance_id")
    if not process_instance_id:
        raise HTTPException(400, "缺少 process_instance_id")

    dry_run = body.get("dry_run", False)
    from dingtalk.oa import create_order_from_oa

    try:
        order = create_order_from_oa(
            process_instance_id=process_instance_id,
            event_id="manual-test",
            dry_run=dry_run,
        )
        if order:
            return {"ok": True, "order_no": order.order_no, "status": order.status,
                    "tracking": order.courier_order_no}
        else:
            return {"ok": False, "msg": "未创建订单（可能已处理过或审批未通过）"}
    except RuntimeError as e:
        return {"ok": False, "msg": str(e)}
    except Exception as e:
        log.exception("[手动触发] 处理异常 instance=%s", process_instance_id)
        return {"ok": False, "msg": "处理失败，请查看服务端日志"}


# ============================================================
# 旧路由保留（向下兼容）
# ============================================================

@router.post("/oa-approval")
async def oa_approval_webhook(request: Request, db: Session = Depends(get_db)):
    """
    旧版 OA 审批回调（兼容之前的直接业务 ID 模式）
    推荐用 /event（正式事件订阅），此路由保留作备用
    """
    raise HTTPException(410, "旧版 OA 回调已停用，请使用带签名校验的 /api/dingtalk/event")
