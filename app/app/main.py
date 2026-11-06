"""医智呼 · AI 随访外呼系统（融合版）API 入口。"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import llm
from .db import SessionLocal, get_db, init_db
from .models import (CallLine, CallResult, CallTask, Patient, Plan, Protocol,
                     Setting, Stage, Ticket, now)
from .seed import seed
from .seed_protocols import match_protocol
from .services import call_service
from .services.plan_service import (business_date, create_plan, get_setting,
                                    on_readmission, plan_view, put_setting,
                                    set_business_date, terminate)

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="医智呼 · AI 随访外呼系统", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

RISK_LABEL = {"blue": "蓝色·平稳", "yellow": "黄色·轻度", "orange": "橙色·中度", "red": "红色·紧急"}


@app.on_event("startup")
def _startup():
    init_db()
    seed()


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


# ---------------------------------------------------------------- 总览
@app.get("/api/overview")
def overview(db: Session = Depends(get_db)):
    bdate = business_date(db)
    total_patients = db.scalar(select(func.count()).select_from(Patient)) or 0
    in_followup = db.scalar(select(func.count()).select_from(Patient).where(Patient.status == "在访")) or 0
    active_plans = db.scalar(select(func.count()).select_from(Plan).where(Plan.status == "active")) or 0
    pending_tickets = db.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == "pending")) or 0

    due = db.scalar(select(func.count()).select_from(CallTask).where(
        CallTask.status == "queued", func.date(CallTask.scheduled_at) <= bdate.isoformat())) or 0
    done_today = db.scalar(select(func.count()).select_from(CallTask).where(
        CallTask.status == "completed", func.date(CallTask.started_at) == bdate.isoformat())) or 0
    total_due = due + done_today
    completion = round(done_today / total_due * 100) if total_due else 0

    risks = dict(db.execute(select(CallResult.risk_level, func.count()).group_by(CallResult.risk_level)).all())
    levels = [{"key": k, "label": RISK_LABEL[k], "value": risks.get(k, 0)} for k in ("red", "orange", "yellow", "blue")]

    trend = []
    for i in range(6, -1, -1):
        d = bdate - timedelta(days=i)
        cnt = db.scalar(select(func.count()).select_from(CallTask).where(
            func.date(CallTask.started_at) == d.isoformat())) or 0
        trend.append({"date": d.strftime("%m-%d"), "value": cnt})

    recent = db.scalars(select(CallTask).where(CallTask.status.in_(["completed", "no_answer"]))
                        .order_by(CallTask.started_at.desc()).limit(6)).all()
    recent_calls = []
    for t in recent:
        r = db.scalars(select(CallResult).where(CallResult.task_id == t.id)).first()
        recent_calls.append({
            "id": t.id, "patient_name": t.patient_name, "stage": t.stage_label,
            "at": _iso(t.started_at), "duration": t.duration, "status": t.status,
            "risk": r.risk_level if r else None, "summary": r.summary if r else "",
        })

    depts = [{"dept": d or "未分科", "value": c} for d, c in
             db.execute(select(Patient.dept, func.count()).group_by(Patient.dept).order_by(func.count().desc()).limit(6)).all()]

    tickets = db.scalars(select(Ticket).where(Ticket.status == "pending")
                         .order_by(Ticket.created_at.desc()).limit(5)).all()
    return {
        "kpi": {"patients": total_patients, "in_followup": in_followup, "active_plans": active_plans,
                "due_today": due, "done_today": done_today, "completion": completion,
                "pending_tickets": pending_tickets},
        "risk": levels, "trend": trend, "recent_calls": recent_calls,
        "depts": depts, "tickets": [ticket_view(t) for t in tickets],
        "business_date": bdate.isoformat(), "llm": llm.available(),
        "line": line_view(db) if db.scalar(select(CallLine).limit(1)) else None,
    }


# ---------------------------------------------------------------- 方案
def protocol_view(p: Protocol, with_stages=True):
    data = {"id": p.id, "name": p.name, "dept": p.dept, "disease_key": p.disease_key,
            "surgery": p.surgery, "status": p.status, "version": p.version,
            "reviewer": p.reviewer, "source": p.source, "note": p.note,
            "red_flags": p.red_flags or [], "updated_at": _iso(p.updated_at),
            "stage_count": len(p.stages or [])}
    if with_stages:
        data["stages"] = p.stages or []
    return data


@app.get("/api/protocols")
def list_protocols(db: Session = Depends(get_db)):
    rows = db.scalars(select(Protocol).order_by(Protocol.created_at)).all()
    return [protocol_view(p) for p in rows]


@app.get("/api/protocols/{pid}")
def get_protocol(pid: str, db: Session = Depends(get_db)):
    p = db.get(Protocol, pid)
    if not p:
        raise HTTPException(404, "未找到方案")
    return protocol_view(p)


@app.patch("/api/protocols/{pid}")
def update_protocol(pid: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    p = db.get(Protocol, pid)
    if not p:
        raise HTTPException(404, "未找到方案")
    for key in ("name", "dept", "disease_key", "surgery", "note", "reviewer"):
        if key in payload:
            setattr(p, key, payload[key])
    if "stages" in payload:
        p.stages = payload["stages"]
        p.status = "reviewing"
        p.version += 1
    db.commit()
    return protocol_view(p)


@app.post("/api/protocols/{pid}/publish")
def publish_protocol(pid: str, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    p = db.get(Protocol, pid)
    if not p:
        raise HTTPException(404, "未找到方案")
    p.status = "published"
    p.reviewer = payload.get("reviewer") or p.reviewer or "医师"
    db.commit()
    return protocol_view(p)


@app.post("/api/protocols/{pid}/generate")
def generate_script(pid: str, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """输入病种/手术/出院天数/关注点，由大模型生成分阶段随访提问（医生审核后发布）。"""
    p = db.get(Protocol, pid)
    if not p:
        raise HTTPException(404, "未找到方案")
    stage_key = payload.get("stage_key")
    stage = next((s for s in (p.stages or []) if s["key"] == stage_key), None) if stage_key else None
    context = {
        "病种": p.disease_key or p.name, "科室": p.dept, "手术": p.surgery or "无",
        "出院天数": (stage or {}).get("day", 7), "阶段": (stage or {}).get("label", "出院随访"),
        "关注点": (stage or {}).get("focus", []),
        "已有问题": [q["text"] for q in (stage or {}).get("questions", [])],
        "要求": "生成 5-7 个口语化、适合电话提问的问题，每个问题带追问分支；避免诱导性提问和诊断性表述。",
    }
    data = llm.chat_json(
        "你是三甲医院随访中心的医生助理，负责把随访规范改写成电话话术。"
        "输出 JSON：{\"questions\":[{\"text\":\"\",\"purpose\":\"\",\"branch\":\"\"}]}。"
        "称呼统一用「您」，单句不超过 40 字，必须先确认身份再问病情。",
        context, max_tokens=1400,
    )
    if not data or not data.get("questions"):
        raise HTTPException(503, "大模型暂时不可用，请稍后重试或手工编辑")
    questions = [{"text": q.get("text", ""), "purpose": q.get("purpose", ""), "branch": q.get("branch", "")}
                 for q in data["questions"] if q.get("text")]
    if not stage:
        raise HTTPException(400, "请指定要生成的阶段")
    for s in p.stages:
        if s["key"] == stage_key:
            s["questions"] = questions
    p.status = "reviewing"
    p.version += 1
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(p, "stages")
    db.commit()
    return {"ok": True, "questions": questions, "provider": "llm", "status": p.status}


# ---------------------------------------------------------------- 患者
def patient_view(p: Patient, db: Session):
    plan = db.scalars(select(Plan).where(Plan.patient_id == p.id)
                      .order_by(Plan.created_at.desc())).first()
    return {
        "id": p.id, "name": p.name, "sex": p.sex, "age": p.age, "phone": p.phone,
        "inpatient_no": p.inpatient_no, "dept": p.dept, "diagnosis": p.diagnosis,
        "surgery": p.surgery, "medications": p.medications,
        "discharge_advice": p.discharge_advice, "review_advice": p.review_advice,
        "discharge_date": _iso(p.discharge_date), "risk_group": p.risk_group,
        "status": p.status, "source": p.source,
        "plan_id": plan.id if plan else None,
        "plan_status": plan.status if plan else None,
        "protocol_name": plan.protocol_name if plan else None,
    }


@app.get("/api/patients")
def list_patients(q: str = "", dept: str = "", status: str = "", page: int = 1,
                  size: int = 20, db: Session = Depends(get_db)):
    stmt = select(Patient)
    if q:
        stmt = stmt.where(Patient.name.contains(q) | Patient.diagnosis.contains(q) |
                          Patient.inpatient_no.contains(q) | Patient.phone.contains(q))
    if dept:
        stmt = stmt.where(Patient.dept == dept)
    if status:
        stmt = stmt.where(Patient.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Patient.created_at.desc()).offset((page - 1) * size).limit(size)).all()
    return {"total": total, "items": [patient_view(p, db) for p in rows],
            "depts": [d for (d,) in db.execute(select(Patient.dept).distinct()).all() if d]}


@app.get("/api/patients/{pid}")
def get_patient(pid: str, db: Session = Depends(get_db)):
    p = db.get(Patient, pid)
    if not p:
        raise HTTPException(404, "未找到患者")
    plans = db.scalars(select(Plan).where(Plan.patient_id == pid).order_by(Plan.created_at.desc())).all()
    calls = db.scalars(select(CallTask).where(CallTask.patient_id == pid)
                       .order_by(CallTask.created_at.desc()).limit(20)).all()
    return {**patient_view(p, db), "plans": [plan_view(db, x) for x in plans],
            "calls": [task_view(db, t) for t in calls]}


@app.post("/api/patients")
def create_patient(payload: dict = Body(...), db: Session = Depends(get_db)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "姓名不能为空")
    dd = payload.get("discharge_date")
    try:
        dd = date.fromisoformat(dd) if dd else business_date(db)
    except ValueError:
        raise HTTPException(422, "出院日期格式应为 YYYY-MM-DD")
    p = Patient(name=name, sex=payload.get("sex", "未知"), age=payload.get("age"),
                phone=payload.get("phone", ""), inpatient_no=payload.get("inpatient_no", ""),
                dept=payload.get("dept", ""), diagnosis=payload.get("diagnosis", ""),
                surgery=payload.get("surgery", ""), medications=payload.get("medications", ""),
                discharge_advice=payload.get("discharge_advice", ""),
                review_advice=payload.get("review_advice", ""),
                discharge_date=dd, risk_group=payload.get("risk_group", "稳定"),
                status="在访", source="医生录入")
    db.add(p)
    db.commit()
    proto = match_protocol(db.scalars(select(Protocol).where(Protocol.status == "published")).all(),
                           p.diagnosis or "", p.dept or "", p.surgery or "")
    plan = create_plan(db, p, proto) if proto else None
    return {**patient_view(p, db), "plan": plan_view(db, plan) if plan else None}


@app.post("/api/patients/{pid}/plan")
def make_plan(pid: str, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    p = db.get(Patient, pid)
    if not p:
        raise HTTPException(404, "未找到患者")
    active = db.scalars(select(Plan).where(Plan.patient_id == pid, Plan.status == "active")).first()
    if active:
        raise HTTPException(409, "该患者已有执行中的随访计划")
    proto = db.get(Protocol, payload["protocol_id"]) if payload.get("protocol_id") else None
    if proto is None:
        proto = match_protocol(db.scalars(select(Protocol).where(Protocol.status == "published")).all(),
                               p.diagnosis or "", p.dept or "", payload.get("surgery") or p.surgery or "")
    if proto is None:
        raise HTTPException(422, "未匹配到随访方案，请先选择病种方案")
    if payload.get("surgery"):
        p.surgery = payload["surgery"]
    p.status = "在访"
    plan = create_plan(db, p, proto)
    return plan_view(db, plan)


@app.post("/api/patients/{pid}/readmission")
def readmission(pid: str, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    plan = on_readmission(db, pid, payload.get("note", ""))
    if not plan:
        raise HTTPException(404, "该患者没有执行中的计划")
    return {"ok": True, "message": "已记录再入院事件，原随访计划按固化版本继续，不做任何改写",
            "plan": plan_view(db, plan)}


# ---------------------------------------------------------------- 计划
@app.get("/api/plans")
def list_plans(status: str = "", db: Session = Depends(get_db)):
    stmt = select(Plan)
    if status:
        stmt = stmt.where(Plan.status == status)
    rows = db.scalars(stmt.order_by(Plan.created_at.desc()).limit(80)).all()
    out = []
    for p in rows:
        v = plan_view(db, p)
        v["done"] = sum(1 for s in v["stages"] if s["status"] == "done")
        v["total"] = len(v["stages"])
        out.append(v)
    return out


@app.get("/api/plans/{pid}")
def get_plan(pid: str, db: Session = Depends(get_db)):
    p = db.get(Plan, pid)
    if not p:
        raise HTTPException(404, "未找到计划")
    return plan_view(db, p)


@app.post("/api/plans/{pid}/terminate")
def terminate_plan(pid: str, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    p = db.get(Plan, pid)
    if not p:
        raise HTTPException(404, "未找到计划")
    terminate(db, p, payload.get("reason") or "医生手动终止随访计划", "已终止")
    return plan_view(db, p)


# ---------------------------------------------------------------- 外呼
def task_view(db: Session, t: CallTask, detail=False):
    r = db.scalars(select(CallResult).where(CallResult.task_id == t.id)).first()
    data = {
        "id": t.id, "patient_id": t.patient_id, "patient_name": t.patient_name,
        "phone": t.phone, "stage": t.stage_label, "plan_id": t.plan_id,
        "scheduled_at": _iso(t.scheduled_at), "started_at": _iso(t.started_at),
        "duration": t.duration, "status": t.status, "attempts": t.attempts,
        "provider": t.provider, "persona": t.persona, "hangup_by": t.hangup_by,
        "risk": r.risk_level if r else None, "summary": r.summary if r else "",
    }
    if detail:
        data["transcript"] = t.transcript or []
        if r:
            data["result"] = {"structured": r.structured, "risk_level": r.risk_level,
                              "risk_reasons": r.risk_reasons, "summary": r.summary,
                              "advice": r.advice, "follow_up": r.follow_up, "engine": r.engine}
        ticket = db.scalars(select(Ticket).where(Ticket.task_id == t.id)).first()
        data["ticket"] = ticket_view(ticket) if ticket else None
        stage = db.get(Stage, t.stage_id)
        data["questions"] = stage.questions if stage else []
    return data


@app.get("/api/calls")
def list_calls(status: str = "", risk: str = "", db: Session = Depends(get_db)):
    stmt = select(CallTask)
    if status:
        stmt = stmt.where(CallTask.status == status)
    rows = db.scalars(stmt.order_by(CallTask.scheduled_at.asc(), CallTask.created_at.desc()).limit(120)).all()
    items = [task_view(db, t) for t in rows]
    if risk:
        items = [x for x in items if x["risk"] == risk]
    counts = dict(db.execute(select(CallTask.status, func.count()).group_by(CallTask.status)).all())
    return {"items": items, "counts": counts}


@app.get("/api/calls/{tid}")
def get_call(tid: str, db: Session = Depends(get_db)):
    t = db.get(CallTask, tid)
    if not t:
        raise HTTPException(404, "未找到通话")
    return task_view(db, t, detail=True)


@app.post("/api/calls/{tid}/run")
def run_call(tid: str, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    t = db.get(CallTask, tid)
    if not t:
        raise HTTPException(404, "未找到通话任务")
    result = call_service.execute(db, t, persona=payload.get("persona"),
                                  use_llm=payload.get("use_llm", True))
    return {**result, "task": task_view(db, t, detail=True)}


@app.post("/api/calls/batch")
def batch_call(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    limit = int(payload.get("limit", 5))
    tasks = call_service.batch_due(db, limit)
    results = []
    for t in tasks:
        results.append(call_service.execute(db, t, persona=payload.get("persona"), use_llm=False))
    return {"ok": True, "count": len(results), "results": results}


@app.post("/api/calls/aliyun-callback")
def aliyun_callback(payload: dict = Body(...), db: Session = Depends(get_db)):
    """阿里云语音回执：录音地址、通话状态、ASR 文本回写。"""
    call_id = payload.get("call_id") or payload.get("CallId")
    task = db.scalars(select(CallTask).where(CallTask.recording_url == call_id)).first()
    if not task:
        return {"ok": False, "message": "未匹配到外呼任务"}
    if payload.get("recording_url"):
        task.recording_url = payload["recording_url"]
    if payload.get("transcript"):
        task.transcript = payload["transcript"]
        analysis = call_service.nlu.analyze(payload["transcript"], task.patient_name)
        r = db.scalars(select(CallResult).where(CallResult.task_id == task.id)).first() or CallResult(task_id=task.id, patient_id=task.patient_id)
        db.add(r)
        r.structured, r.risk_level, r.risk_reasons = analysis["structured"], analysis["risk_level"], analysis["risk_reasons"]
        r.summary, r.advice, r.follow_up, r.engine = analysis["summary"], analysis["advice"], analysis["follow_up"], analysis["engine"]
        call_service._make_ticket(db, task, r)
        stage = db.get(Stage, task.stage_id)
        if stage:
            stage.status = "done"
        task.status = "completed"
        task.duration = int(payload.get("duration", 0))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 工单
def ticket_view(t: Ticket):
    return {"id": t.id, "patient_id": t.patient_id, "patient_name": t.patient_name,
            "task_id": t.task_id, "level": t.level, "category": t.category,
            "reason": t.reason, "suggestion": t.suggestion, "status": t.status,
            "sla_hours": t.sla_hours, "due_at": _iso(t.due_at),
            "doctor_note": t.doctor_note, "created_at": _iso(t.created_at),
            "closed_at": _iso(t.closed_at)}


@app.get("/api/tickets")
def list_tickets(status: str = "", db: Session = Depends(get_db)):
    stmt = select(Ticket)
    if status:
        stmt = stmt.where(Ticket.status == status)
    rows = db.scalars(stmt.order_by(Ticket.created_at.desc()).limit(60)).all()
    counts = dict(db.execute(select(Ticket.level, func.count()).group_by(Ticket.level)).all())
    return {"items": [ticket_view(t) for t in rows], "counts": counts}


@app.post("/api/tickets/{tid}/handle")
def handle_ticket(tid: str, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    t = db.get(Ticket, tid)
    if not t:
        raise HTTPException(404, "未找到工单")
    t.status = payload.get("status", "closed")
    t.doctor_note = payload.get("doctor_note", "")
    if t.status == "closed":
        t.closed_at = now()
    db.commit()
    return ticket_view(t)


# ---------------------------------------------------------------- 设置
def line_view(db: Session):
    line = db.scalars(select(CallLine)).first()
    if not line:
        return None
    from .providers import aliyun
    return {"id": line.id, "name": line.name, "provider": line.provider,
            "caller_number": line.caller_number, "concurrent": line.concurrent,
            "enabled": line.enabled, "mode": line.mode, "whitelist": line.whitelist,
            "aliyun_configured": aliyun.configured()}


@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    return {"business_date": business_date(db).isoformat(), "line": line_view(db),
            "llm": {"available": llm.available(), "model": llm.MODEL},
            "db": str(ROOT / "data" / "yizhihu.db")}


@app.put("/api/settings")
def update_settings(payload: dict = Body(...), db: Session = Depends(get_db)):
    if payload.get("business_date"):
        set_business_date(db, payload["business_date"])
    line = db.scalars(select(CallLine)).first()
    if line and "line" in payload:
        data = payload["line"]
        for key in ("name", "caller_number", "mode", "concurrent"):
            if key in data:
                setattr(line, key, data[key])
        if "whitelist" in data:
            line.whitelist = [w.strip() for w in data["whitelist"] if w.strip()]
        if "enabled" in data:
            line.enabled = bool(data["enabled"])
        db.commit()
    return get_settings(db)


# ---------------------------------------------------------------- 导入
@app.post("/api/import/patients")
async def import_patients(file: UploadFile = File(...), db: Session = Depends(get_db)):
    raw = await file.read()
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    except Exception:
        raise HTTPException(422, "请上传 .xlsx 文件")
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise HTTPException(422, "表格为空")
    header = [str(c or "") for c in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    created, skipped = 0, 0
    for r in rows[1:]:
        def get(*names):
            for n in names:
                if n in idx and idx[n] < len(r) and r[idx[n]] not in (None, ""):
                    return r[idx[n]]
            return ""
        name = str(get("姓名", "患者姓名", "NAME")).strip()
        if not name:
            continue
        no = str(get("住院号", "PATIENT_ID", "住院号 "))
        if no and db.scalar(select(Patient).where(Patient.inpatient_no == no)):
            skipped += 1
            continue
        dd = get("出院日期", "DISCHARGE_DATE_TIME", "出院时间")
        try:
            dd = date.fromisoformat(str(dd)[:10]) if dd else business_date(db)
        except ValueError:
            dd = business_date(db)
        p = Patient(name=name, sex=str(get("性别", "SEX") or "未知")[:4],
                    age=int(get("年龄")) if str(get("年龄")).isdigit() else None,
                    phone=str(get("联系电话", "电话", "MAILA") or ""),
                    inpatient_no=no, dept=str(get("科室", "DEPT_NAME") or ""),
                    diagnosis=str(get("诊断", "主要诊断") or ""), surgery=str(get("手术", "术式") or ""),
                    discharge_date=dd, risk_group=str(get("人群分类", "风险分层") or "稳定"),
                    status="在访", source=f"导入：{file.filename}")
        db.add(p)
        created += 1
    db.commit()
    return {"ok": True, "created": created, "skipped": skipped,
            "message": f"导入 {created} 人，跳过重复 {skipped} 人；可在患者列表逐一生成计划"}


@app.get("/api/llm/health")
def llm_health():
    return llm.health()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "yizhihu", "time": now().isoformat()}


# ---------------------------------------------------------------- 静态资源
static_dir = ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def index():
    return FileResponse(static_dir / "index.html")


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "Not Found")
    target = static_dir / path
    if target.exists() and target.is_file():
        return FileResponse(target)
    return JSONResponse({"detail": "Not Found"}, status_code=404)
