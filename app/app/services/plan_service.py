"""随访计划服务：分阶段排期 + 计划固化 + 脱访终止。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CallTask, Patient, Plan, Protocol, Setting, Stage, now


def business_date(db: Session) -> date:
    row = db.get(Setting, "business_date")
    if row and row.value:
        try:
            return date.fromisoformat(row.value)
        except ValueError:
            pass
    return date.today()


def set_business_date(db: Session, value: str):
    row = db.get(Setting, "business_date")
    if row:
        row.value = value
    else:
        db.add(Setting(key="business_date", value=value))
    db.commit()


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def put_setting(db: Session, key: str, value: str):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def _render(text: str, patient: Patient) -> str:
    honor = "女士" if patient.sex == "女" else "先生"
    name = patient.name.split("*")[0].split("X")[0]
    name = f"{name}{honor}" if name and len(name) <= 6 else patient.name
    return text.replace("{姓名}", name).replace("{医院}", "本院")


def stage_offsets(protocol: Protocol, risk_group: str) -> list[dict]:
    """按风险分层筛选阶段：稳定患者跳过 3/14 天加密节点。"""
    high = "高危" in (risk_group or "")
    out = []
    for s in protocol.stages:
        day = s.get("high_risk_day" if high else "day") or s.get("day")
        if not high and s["key"] in ("D3", "D14"):
            continue
        out.append({**s, "day": int(day)})
    return out


def create_plan(db: Session, patient: Patient, protocol: Protocol | None = None, bdate: date | None = None) -> Plan:
    """创建即固化：阶段日期、问题清单全部快照，之后不再跟随患者资料变化。"""
    from ..seed_protocols import match_protocol
    bdate = bdate or business_date(db)
    if protocol is None:
        protocol = match_protocol(db.scalars(select(Protocol).where(Protocol.status == "published")).all(),
                                  patient.diagnosis or "", patient.dept or "", patient.surgery or "")
    discharge = patient.discharge_date or bdate

    plan = Plan(
        patient_id=patient.id,
        protocol_id=protocol.id if protocol else "",
        protocol_version=protocol.version if protocol else 0,
        protocol_name=protocol.name if protocol else "通用出院随访",
        diagnosis_snapshot=patient.diagnosis or "",
        discharge_date=discharge,
        risk_group=patient.risk_group or "稳定",
        status="active",
        frozen=True,
        frozen_at=now(),
        events=[{"at": now().isoformat(), "type": "created",
                 "text": f"按「{protocol.name if protocol else '通用出院随访'}」生成计划并固化"}],
    )
    db.add(plan)
    db.flush()

    offsets = stage_offsets(protocol, patient.risk_group) if protocol else [
        {"key": "D7", "label": "出院 7 天", "day": 7, "focus": ["恢复情况"], "questions": []},
        {"key": "D30", "label": "出院 30 天", "day": 30, "focus": ["复查"], "questions": []},
    ]
    for s in offsets:
        sched = discharge + timedelta(days=int(s["day"]))
        st = Stage(
            plan_id=plan.id, patient_id=patient.id, key=s["key"], label=s.get("label") or s["key"],
            day_offset=int(s["day"]), scheduled_date=sched,
            focus=s.get("focus") or [],
            questions=[{"text": _render(q["text"], patient), "purpose": q.get("purpose", ""),
                        "branch": q.get("branch", "")} for q in s.get("questions", [])],
            locked=True,
            status="pending",
        )
        db.add(st)
        db.flush()
        db.add(CallTask(
            stage_id=st.id, plan_id=plan.id, patient_id=patient.id, patient_name=patient.name,
            phone=patient.phone, stage_label=st.label,
            scheduled_at=datetime.combine(sched, datetime.min.time()).replace(hour=9),
        ))
    db.commit()
    return plan


def add_event(db: Session, plan: Plan, kind: str, text: str):
    events = list(plan.events or [])
    events.append({"at": now().isoformat(), "type": kind, "text": text})
    plan.events = events
    db.commit()


def terminate(db: Session, plan: Plan, reason: str, patient_status: str = "已终止"):
    """脱访/转院：不改写历史，只终止后续节点。"""
    plan.status = "terminated"
    plan.terminate_reason = reason
    for st in db.scalars(select(Stage).where(Stage.plan_id == plan.id)).all():
        if st.status == "pending":
            st.status = "terminated"
    for task in db.scalars(select(CallTask).where(CallTask.plan_id == plan.id,
                                                  CallTask.status == "queued")).all():
        task.status = "terminated"
    patient = db.get(Patient, plan.patient_id)
    if patient:
        patient.status = patient_status
    add_event(db, plan, "terminated", reason)
    db.commit()


def on_readmission(db: Session, patient_id: str, note: str):
    """患者再次入院 / 新医嘱：已固化计划保持不变，只记录事件。"""
    plan = db.scalars(select(Plan).where(Plan.patient_id == patient_id,
                                         Plan.status == "active")).first()
    if not plan:
        return None
    add_event(db, plan, "readmission", note or "患者再次入院，原随访计划按固化版本继续执行")
    return plan


def plan_view(db: Session, plan: Plan, bdate: date | None = None) -> dict:
    bdate = bdate or business_date(db)
    patient = db.get(Patient, plan.patient_id)
    stages = db.scalars(select(Stage).where(Stage.plan_id == plan.id)
                        .order_by(Stage.day_offset)).all()
    tasks = {t.stage_id: t for t in db.scalars(select(CallTask).where(CallTask.plan_id == plan.id)).all()}
    today = bdate
    return {
        "id": plan.id, "patient_id": plan.patient_id,
        "patient_name": patient.name if patient else "",
        "dept": patient.dept if patient else "",
        "phone": patient.phone if patient else "",
        "protocol_name": plan.protocol_name, "risk_group": plan.risk_group,
        "discharge_date": plan.discharge_date.isoformat() if plan.discharge_date else None,
        "status": plan.status, "frozen": plan.frozen,
        "frozen_at": plan.frozen_at.isoformat() if plan.frozen_at else None,
        "terminate_reason": plan.terminate_reason,
        "events": plan.events or [],
        "stages": [{
            "id": s.id, "key": s.key, "label": s.label, "day_offset": s.day_offset,
            "scheduled_date": s.scheduled_date.isoformat() if s.scheduled_date else None,
            "focus": s.focus, "questions": s.questions, "status": s.status, "locked": s.locked,
            "due": (s.scheduled_date <= today) if s.scheduled_date else False,
            "task_id": tasks[s.id].id if s.id in tasks else None,
            "task_status": tasks[s.id].status if s.id in tasks else None,
        } for s in stages],
    }
