"""初始化演示数据：方案 + 患者 + 计划 + 历史通话 + 工单。"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from sqlalchemy import select

from .db import ROOT, SessionLocal, init_db
from .models import (CallLine, CallResult, CallTask, Patient, Plan, Protocol,
                     Setting, Stage, Ticket)
from .seed_protocols import PROTOCOLS, match_protocol
from .services import call_service
from .services.plan_service import business_date, create_plan


def _load_patients():
    path = ROOT / "data" / "seed" / "patients.json"
    if not path.exists():
        return {"diabetes": [], "his": []}
    return json.loads(path.read_text(encoding="utf-8"))


def seed(force: bool = False):
    init_db()
    db = SessionLocal()
    try:
        if force:
            for model in (None,):
                pass
        if db.scalar(select(Protocol).limit(1)) is None:
            for p in PROTOCOLS:
                db.add(Protocol(name=p["name"], dept=p["dept"], disease_key=p["disease_key"],
                                surgery=p["surgery"], stages=p["stages"], red_flags=p["red_flags"],
                                status=p["status"], reviewer=p["reviewer"], source=p["source"],
                                note=p["note"], version=1))
            db.commit()

        if db.get(Setting, "business_date") is None:
            db.add(Setting(key="business_date", value=date.today().isoformat()))
        if db.scalar(select(CallLine).limit(1)) is None:
            db.add(CallLine(name="阿里云语音双呼专线", provider="aliyun", caller_number="0571-88888888",
                            concurrent=5, enabled=True, mode="mock",
                            whitelist=["13800000001", "13800000002", "13800000005"]))
        db.commit()

        if db.scalar(select(Patient).limit(1)) is not None:
            return

        bdate = business_date(db)
        data = _load_patients()
        made: list[Patient] = []

        for idx, item in enumerate(data.get("diabetes", [])):
            p = Patient(
                name=item["name"], sex=item["sex"], age=item["age"], phone=item["phone"],
                inpatient_no=item["inpatient_no"], dept=item["dept"], diagnosis=item["diagnosis"],
                surgery=item.get("surgery", ""), medications=item.get("medications", ""),
                discharge_advice=item.get("discharge_advice", ""), review_advice=item.get("review_advice", ""),
                discharge_date=bdate - timedelta(days=item.get("offset_days", 7)),
                risk_group=item.get("risk_group", "稳定"), status="在访", source=item.get("source", ""),
            )
            db.add(p)
            made.append(p)
        db.commit()

        # HIS 出院患者：保留真实科室与诊断，用于演示「导入→自动匹配病种」
        for item in data.get("his", []):
            d = item.get("discharge_date") or ""
            try:
                dd = date.fromisoformat(d)
            except ValueError:
                dd = bdate - timedelta(days=7)
            p = Patient(
                name=item["name"], sex=item["sex"], age=item["age"], phone=item["phone"],
                inpatient_no=item["inpatient_no"], dept=item["dept"], diagnosis=item["diagnosis"],
                discharge_date=dd, risk_group="稳定", status="在访", source=item.get("source", ""),
            )
            db.add(p)
            made.append(p)
        db.commit()

        published = db.scalars(select(Protocol).where(Protocol.status == "published")).all()
        plans: list[Plan] = []
        # 糖尿病 25 人全部建计划；HIS 患者中可匹配方案的也建计划
        for p in made:
            proto = match_protocol(published, p.diagnosis or "", p.dept or "", p.surgery or "")
            if not proto:
                p.status = "待建档"
                continue
            plans.append(create_plan(db, p, proto, bdate))

        # 历史节点补通话记录，让工作台有真实分布
        personas = ["稳定", "漏服", "血糖偏高", "稳定", "足部麻木", "低血糖", "自行停药", "足部破溃", "酮症", "稳定", "拒接", "转院"]
        i = 0
        for plan in plans:
            stages = db.scalars(select(Stage).where(Stage.plan_id == plan.id)
                                .order_by(Stage.day_offset)).all()
            for st in stages:
                if st.scheduled_date and st.scheduled_date < bdate:
                    task = db.scalars(select(CallTask).where(CallTask.stage_id == st.id)).first()
                    if not task:
                        continue
                    persona = personas[i % len(personas)]
                    i += 1
                    call_service.execute(db, task, persona=persona, use_llm=False)
                    if task.started_at is not None and st.scheduled_date:
                        at = datetime.combine(st.scheduled_date, datetime.min.time()).replace(hour=9, minute=30)
                        task.started_at = at
                        task.created_at = at
                        res = db.scalars(select(CallResult).where(CallResult.task_id == task.id)).first()
                        if res:
                            res.created_at = at
                        tk = db.scalars(select(Ticket).where(Ticket.task_id == task.id)).first()
                        if tk:
                            tk.created_at = at
                            tk.due_at = at + timedelta(hours=tk.sla_hours)
                    db.commit()

        # 一条已脱访（转院）样例
        p = db.scalars(select(Patient).where(Patient.status == "在访").limit(1)).first()
        plan = db.scalars(select(Plan).where(Plan.patient_id == p.id)).first() if p else None
        if plan:
            pending = db.scalars(select(Stage).where(Stage.plan_id == plan.id,
                                                     Stage.status == "pending")).all()
            if pending:
                from .services.plan_service import terminate
                terminate(db, plan, "通话中患者家属表示已转往省立医院继续治疗，按脱访规则自动终止计划", "转院")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    seed()
    print("seed done")
