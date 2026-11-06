"""外呼执行服务：任务 → 通话 → 转写 → 结构化 → 风险 → 工单 → 计划状态回写。"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CallLine, CallResult, CallTask, Patient, Plan, Stage, Ticket, now
from ..providers import aliyun
from . import nlu, simulator
from .plan_service import add_event, business_date, terminate

SLA = {"red": 12, "orange": 24, "yellow": 24, "blue": 72}


def active_line(db: Session) -> CallLine | None:
    return db.scalars(select(CallLine).where(CallLine.enabled == True)).first()  # noqa: E712


def _make_ticket(db: Session, task: CallTask, result: CallResult) -> Ticket | None:
    if result.risk_level == "blue":
        return None
    existing = db.scalars(select(Ticket).where(Ticket.task_id == task.id)).first()
    if existing:
        return existing
    hours = SLA.get(result.risk_level, 24)
    ticket = Ticket(
        patient_id=task.patient_id, patient_name=task.patient_name, task_id=task.id,
        level=result.risk_level, category="异常随访",
        reason="；".join(result.risk_reasons or []), suggestion=result.advice,
        status="pending", sla_hours=hours, due_at=now() + timedelta(hours=hours),
    )
    db.add(ticket)
    return ticket


def execute(db: Session, task: CallTask, persona: str | None = None, use_llm: bool = True) -> dict:
    """执行一次外呼。mock 为同步仿真；aliyun 为真实发起（异步等回执）。"""
    patient = db.get(Patient, task.patient_id)
    stage = db.get(Stage, task.stage_id)
    line = active_line(db)
    if not patient or not stage:
        raise ValueError("随访任务关联的患者或阶段不存在")

    if task.status in ("completed", "terminated"):
        return {"ok": False, "message": "该任务已完成或已终止"}

    provider = line.mode if line else "mock"
    # 真实外呼前置校验：白名单 + 线路 + 密钥
    if provider == "real":
        if not aliyun.configured():
            return {"ok": False, "message": "未配置阿里云 AccessKey，已保持演练模式。请在设置中填写密钥或切回演练模式。"}
        if line and line.whitelist and task.phone not in line.whitelist:
            return {"ok": False, "message": f"号码 {task.phone} 不在外呼白名单内，已拦截。可在设置中维护白名单。"}
        try:
            resp = aliyun.single_call_tts(task.phone, line.caller_number, "TTS_000000",
                                          '{"name":"%s"}' % task.patient_name)
        except Exception as exc:
            return {"ok": False, "message": f"阿里云外呼失败：{exc}"}
        task.status = "calling"
        task.provider = "aliyun"
        task.attempts += 1
        task.started_at = now()
        task.transcript = [{"speaker": "系统", "text": f"已提交阿里云外呼，CallId={resp.get('CallId', '')}", "second": 0}]
        task.recording_url = str(resp.get("CallId", ""))
        db.commit()
        return {"ok": True, "mode": "real", "message": "已发起真实外呼，等待运营商回执与录音转写", "call_id": resp.get("CallId")}

    # 演练模式
    task.provider = "mock"
    task.attempts += 1
    task.started_at = now()
    task.status = "calling"

    if persona == "拒接":
        task.status = "no_answer"
        task.hangup_by = "未接听"
        task.transcript = [{"speaker": "系统", "text": "对方无人接听 / 直接挂断", "second": 0}]
        task.duration = 0
        _after_no_answer(db, task, patient)
        db.commit()
        return {"ok": True, "mode": "mock", "status": "no_answer", "message": "未接通，已进入重拨队列"}

    sim = simulator.simulate(patient, stage, persona)
    task.transcript = sim["transcript"]
    task.duration = sim["duration"]
    task.persona = sim["persona"]
    task.status = "completed"
    task.hangup_by = "患者"

    analysis = nlu.analyze(sim["transcript"], patient.name, use_llm=use_llm)
    result = db.scalars(select(CallResult).where(CallResult.task_id == task.id)).first()
    if not result:
        result = CallResult(task_id=task.id, patient_id=patient.id)
        db.add(result)
    result.structured = analysis["structured"]
    result.risk_level = analysis["risk_level"]
    result.risk_reasons = analysis["risk_reasons"]
    result.summary = analysis["summary"]
    result.advice = analysis["advice"]
    result.follow_up = analysis["follow_up"]
    result.engine = analysis["engine"]

    stage.status = "done"
    plan = db.get(Plan, task.plan_id)
    ticket = _make_ticket(db, task, result)

    if analysis["structured"].get("transferred"):
        if plan:
            terminate(db, plan, f"通话中患者表示转往其他医院就诊（{task.stage_label}随访），按脱访规则终止计划", "转院")
    elif plan:
        remain = db.scalars(select(Stage).where(Stage.plan_id == plan.id,
                                                Stage.status == "pending")).all()
        if not remain:
            plan.status = "completed"
            add_event(db, plan, "completed", "全部随访节点已完成")

    db.commit()
    return {
        "ok": True, "mode": "mock", "status": "completed",
        "task_id": task.id, "risk_level": result.risk_level,
        "risk_reasons": result.risk_reasons, "summary": result.summary,
        "ticket_id": ticket.id if ticket else None, "engine": result.engine,
    }


def _after_no_answer(db: Session, task: CallTask, patient: Patient):
    """未接通重拨策略：30 分钟 → 次日同时段 → 连续 5 次转失联。"""
    if task.attempts >= 5:
        plan = db.get(Plan, task.plan_id)
        if plan:
            terminate(db, plan, "连续 5 次呼叫未接通，判定为失联患者，转护士人工跟进", "失联")
        return
    delay = timedelta(minutes=30) if task.attempts == 1 else timedelta(days=1)
    task.scheduled_at = now() + delay


def end_call_by_patient(db: Session, task: CallTask, reason: str = "患者中途挂断"):
    task.status = "no_answer"
    task.hangup_by = "患者"
    task.transcript = (task.transcript or []) + [{"speaker": "系统", "text": reason, "second": task.duration or 0}]
    db.commit()
    return {"ok": True}


def batch_due(db: Session, limit: int = 10):
    bdate = business_date(db)
    tasks = db.scalars(
        select(CallTask).where(CallTask.status == "queued")
        .order_by(CallTask.scheduled_at).limit(limit)
    ).all()
    return [t for t in tasks if t.scheduled_at and t.scheduled_at.date() <= bdate]
