"""医智呼融合版数据模型。

融合来源：
- medical-followup（患者 / 随访任务 / 会话 / 风险 / 工单）：领域语义与状态机
- ai-calling-plat（线路 / 话术 / 任务 / 运行批次 / 通话 / 转写）：外呼执行链路
两者统一到一张患者-计划-阶段-外呼-结果的闭环上，避免双份患者表。
"""
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def nid(prefix=""):
    return f"{prefix}{uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class Protocol(TimestampMixin, Base):
    """病种随访方案（医生编排的最小单元）。"""
    __tablename__ = "protocol"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: nid("PR"))
    name: Mapped[str] = mapped_column(String(120))
    dept: Mapped[str] = mapped_column(String(80), default="")
    disease_key: Mapped[str] = mapped_column(String(40), default="")   # 匹配用关键词
    surgery: Mapped[str] = mapped_column(String(160), default="")
    # [{key,label,day,high_risk_day,focus:[],questions:[{text,purpose,branch}],tests:[]}]
    stages: Mapped[list] = mapped_column(JSON, default=list)
    red_flags: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|reviewing|published
    version: Mapped[int] = mapped_column(Integer, default=1)
    reviewer: Mapped[str] = mapped_column(String(40), default="")
    source: Mapped[str] = mapped_column(String(40), default="builtin")
    note: Mapped[str] = mapped_column(Text, default="")


class Patient(TimestampMixin, Base):
    __tablename__ = "patient"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: nid("P"))
    name: Mapped[str] = mapped_column(String(60), index=True)
    sex: Mapped[str] = mapped_column(String(8), default="未知")
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    phone: Mapped[str] = mapped_column(String(30), default="")
    inpatient_no: Mapped[str] = mapped_column(String(40), default="", index=True)
    dept: Mapped[str] = mapped_column(String(80), default="")
    diagnosis: Mapped[str] = mapped_column(Text, default="")
    surgery: Mapped[str] = mapped_column(String(160), default="")
    medications: Mapped[str] = mapped_column(Text, default="")
    discharge_advice: Mapped[str] = mapped_column(Text, default="")
    review_advice: Mapped[str] = mapped_column(Text, default="")
    discharge_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    risk_group: Mapped[str] = mapped_column(String(20), default="稳定")  # 稳定|高危并发症
    status: Mapped[str] = mapped_column(String(20), default="在访")      # 在访|已终止|已完成|转院
    source: Mapped[str] = mapped_column(String(40), default="手工录入")
    note: Mapped[str] = mapped_column(Text, default="")


class Plan(TimestampMixin, Base):
    """出院随访计划。创建即固化：快照 + 不可变阶段，后续再入院不改写。"""
    __tablename__ = "plan"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: nid("PL"))
    patient_id: Mapped[str] = mapped_column(ForeignKey("patient.id"), index=True)
    protocol_id: Mapped[str] = mapped_column(String(32), default="")
    protocol_version: Mapped[int] = mapped_column(Integer, default=1)
    protocol_name: Mapped[str] = mapped_column(String(120), default="")
    diagnosis_snapshot: Mapped[str] = mapped_column(Text, default="")
    discharge_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    risk_group: Mapped[str] = mapped_column(String(20), default="稳定")
    status: Mapped[str] = mapped_column(String(24), default="active")  # active|frozen|terminated|completed
    frozen: Mapped[bool] = mapped_column(Boolean, default=True)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terminate_reason: Mapped[str] = mapped_column(Text, default="")
    events: Mapped[list] = mapped_column(JSON, default=list)  # 固化后的变更流水


class Stage(TimestampMixin, Base):
    """计划内的一个随访节点。固化后 questions/date 不再随患者资料变化。"""
    __tablename__ = "stage"
    __table_args__ = (UniqueConstraint("plan_id", "key", name="uq_plan_stage"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: nid("ST"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("plan.id"), index=True)
    patient_id: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(40))
    day_offset: Mapped[int] = mapped_column(Integer)
    scheduled_date: Mapped[date | None] = mapped_column(Date, index=True)
    focus: Mapped[list] = mapped_column(JSON, default=list)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    locked: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|calling|done|skipped|terminated


class CallTask(TimestampMixin, Base):
    __tablename__ = "call_task"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: nid("CALL"))
    stage_id: Mapped[str] = mapped_column(ForeignKey("stage.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String(32), index=True)
    patient_id: Mapped[str] = mapped_column(String(32), index=True)
    patient_name: Mapped[str] = mapped_column(String(60), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    stage_label: Mapped[str] = mapped_column(String(40), default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    # queued|calling|completed|no_answer|failed|terminated
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str] = mapped_column(String(20), default="mock")  # mock|aliyun
    transcript: Mapped[list] = mapped_column(JSON, default=list)
    recording_url: Mapped[str] = mapped_column(Text, default="")
    persona: Mapped[str] = mapped_column(String(40), default="稳定")
    hangup_by: Mapped[str] = mapped_column(String(20), default="")


class CallResult(TimestampMixin, Base):
    __tablename__ = "call_result"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: nid("R"))
    task_id: Mapped[str] = mapped_column(ForeignKey("call_task.id"), unique=True, index=True)
    patient_id: Mapped[str] = mapped_column(String(32), index=True)
    structured: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_level: Mapped[str] = mapped_column(String(12), default="blue")  # blue/yellow/orange/red
    risk_reasons: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    advice: Mapped[str] = mapped_column(Text, default="")
    follow_up: Mapped[str] = mapped_column(Text, default="")
    engine: Mapped[str] = mapped_column(String(20), default="rule")  # llm|rule


class Ticket(TimestampMixin, Base):
    __tablename__ = "ticket"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: nid("SF"))
    patient_id: Mapped[str] = mapped_column(String(32), index=True)
    patient_name: Mapped[str] = mapped_column(String(60), default="")
    task_id: Mapped[str] = mapped_column(String(32), default="")
    level: Mapped[str] = mapped_column(String(12), default="yellow")
    category: Mapped[str] = mapped_column(String(30), default="异常随访")
    reason: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|processing|closed
    sla_hours: Mapped[int] = mapped_column(Integer, default=24)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    doctor_note: Mapped[str] = mapped_column(Text, default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CallLine(TimestampMixin, Base):
    """外呼线路，融合 ai-calling-plat 的 trunk 概念，对接阿里云语音双呼。"""
    __tablename__ = "call_line"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: nid("LN"))
    name: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(20), default="aliyun")
    caller_number: Mapped[str] = mapped_column(String(30), default="")
    concurrent: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(12), default="mock")  # mock|real
    whitelist: Mapped[list] = mapped_column(JSON, default=list)


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
