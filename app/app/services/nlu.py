"""通话结果解析：转写文本 → 结构化字段 → 四级风险 → 随访建议。

医学安全约定：
- 大模型只做「信息抽取与归纳」，风险分级由确定性规则引擎最终裁决，
  避免模型幻觉直接把患者判成低风险而漏报。
- 抽取不出数值就留空，绝不猜测补全。
"""
from __future__ import annotations

import re

from .. import llm

SYMPTOM_WORDS = {
    "手足麻木": ["麻木", "发麻", "踩棉花"],
    "足部破溃": ["破", "溃疡", "渗水", "化脓", "发黑", "伤口不愈"],
    "低血糖": ["心慌", "出冷汗", "手抖", "发软", "冒冷汗"],
    "口干多饮": ["口干", "口渴", "喝水多", "起夜"],
    "消化道症状": ["恶心", "呕吐", "肚子疼", "腹胀", "腹泻"],
    "水肿": ["浮肿", "水肿", "腿肿", "眼皮肿"],
    "泡沫尿": ["泡沫尿", "泡沫很久", "泡沫不散"],
    "胸闷胸痛": ["胸闷", "胸痛", "憋气", "气短"],
    "视物模糊": ["视物模糊", "看东西模糊", "黑影", "眼花"],
    "发热": ["发烧", "发热", "怕冷", "寒战"],
    "咳嗽咳痰": ["咳嗽", "咳痰", "黄痰"],
    "咯血": ["咯血", "咳血", "血痰"],
}

RED_FLAGS = ["昏迷", "意识模糊", "叫不醒", "剧痛", "发黑", "化脓", "大量咯血", "胸痛", "呼吸困难"]


def _grab_number(text: str, anchors: list[str]) -> float | None:
    for anchor in anchors:
        for m in re.finditer(anchor, text):
            window = text[m.start(): m.start() + 26]
            num = re.search(r"(\d{1,2}(?:\.\d)?)", window)
            if num:
                try:
                    val = float(num.group(1))
                    if 1 <= val <= 40:
                        return val
                except ValueError:
                    pass
    nums = re.findall(r"(\d{1,2}\.\d)", text)
    return float(nums[0]) if nums else None


def rule_extract(transcript: list[dict]) -> dict:
    patient_text = " ".join(t["text"] for t in transcript if t["speaker"] == "患者")
    all_text = " ".join(t["text"] for t in transcript)

    symptoms = [name for name, words in SYMPTOM_WORDS.items() if any(w in patient_text for w in words)]

    if any(w in patient_text for w in ("停了", "停了快一周", "自行停", "没吃药", "药也停了")):
        adherence = "自行停药"
    elif any(w in patient_text for w in ("忘记", "漏", "想起来才吃", "有时候", "推迟")):
        adherence = "部分依从"
    elif any(w in patient_text for w in ("按时", "没落下", "规律")):
        adherence = "完全依从"
    else:
        adherence = "未明确"

    identity = "家属代接" if any(w in all_text for w in ("女儿", "儿子", "老伴", "家属", "替他", "替他答")) else "本人"
    if any(w in patient_text for w in ("转院", "转到", "转去", "省医院", "中心医院", "不在这儿看了")):
        identity = identity  # 保留
    transferred = any(w in patient_text for w in ("转院", "转到", "转去", "转上级医院"))

    if any(w in patient_text for w in ("已经约", "约上了", "复查了", "做了", "下周去")):
        recheck = "已预约/已完成"
    elif any(w in patient_text for w in ("没约", "还没", "没去", "没做")):
        recheck = "未落实"
    else:
        recheck = "未提及"

    return {
        "identity": identity,
        "adherence": adherence,
        "fasting_glucose": _grab_number(patient_text, ["空腹"]),
        "postprandial_glucose": _grab_number(patient_text, ["餐后"]),
        "symptoms": symptoms,
        "adverse_effects": [s for s in symptoms if s == "消化道症状"],
        "recheck": recheck,
        "transferred": transferred,
        "hypoglycemia": "低血糖" in symptoms,
        "foot_lesion": "足部破溃" in symptoms,
        "quotes": [t["text"] for t in transcript if t["speaker"] == "患者"][:6],
    }


def _as_float(v):
    """把血糖值统一成 float。

    规则引擎给的是数值，但大模型可能返回 "13.5"、"13.5 mmol/L"、"偏高" 等字符串，
    直接比较会抛 TypeError 导致外呼接口 500。取不到数值就返回 None（按「未抽取到」处理）。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def grade_risk(s: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    fasting, pp = _as_float(s.get("fasting_glucose")), _as_float(s.get("postprandial_glucose"))
    symptoms = s.get("symptoms") or []
    text = " ".join(s.get("quotes") or [])

    # 红色：危及生命
    if fasting and fasting >= 16.7:
        reasons.append(f"空腹血糖 {fasting} mmol/L，达到危急值")
    if "足部破溃" in symptoms and any(w in text for w in ("化脓", "发黑", "剧痛", "味道很重")):
        reasons.append("足部破溃伴化脓/发黑/异味，糖尿病足感染风险")
    if "胸闷胸痛" in symptoms:
        reasons.append("主诉胸闷/胸痛，需排除急性冠脉事件")
    if "咯血" in symptoms:
        reasons.append("咯血，需排除大咯血")
    if any(w in text for w in ("意识模糊", "迷糊", "叫不醒", "昏迷")):
        reasons.append("意识状态改变")
    if "低血糖" in symptoms and any(w in text for w in ("晕倒", "昏迷", "叫不醒")):
        reasons.append("严重低血糖事件")
    if reasons:
        return "red", reasons

    # 橙色：需限期门诊
    if fasting and fasting > 9:
        reasons.append(f"空腹血糖 {fasting} mmol/L 持续偏高")
    if pp and pp > 13:
        reasons.append(f"餐后血糖 {pp} mmol/L 偏高")
    if s.get("adherence") == "自行停药":
        reasons.append("自行停用降糖/治疗药物，存在血糖反弹风险")
    if s.get("foot_lesion"):
        reasons.append("足部破溃未愈合")
    if "手足麻木" in symptoms:
        reasons.append("手足麻木，周围神经病变进展")
    if "水肿" in symptoms or "泡沫尿" in symptoms:
        reasons.append("水肿/泡沫尿，需排查糖尿病肾病")
    if "视物模糊" in symptoms:
        reasons.append("视物模糊，需眼底检查")
    if "发热" in symptoms:
        reasons.append("发热，需排查感染")
    if "低血糖" in symptoms:
        reasons.append("发生低血糖事件，需调整降糖方案")
    if s.get("recheck") == "未落实":
        reasons.append("复查未落实")
    if reasons:
        return "orange", reasons

    # 黄色：居家调整
    if s.get("adherence") == "部分依从":
        reasons.append("存在漏服/间断用药")
    if fasting and 7.0 <= fasting <= 9.0:
        reasons.append(f"空腹血糖 {fasting} mmol/L 轻度偏高")
    if pp and 10.0 < pp <= 13.0:
        reasons.append(f"餐后血糖 {pp} mmol/L 轻度偏高")
    if symptoms:
        reasons.append("轻度不适：" + "、".join(symptoms))
    if reasons:
        return "yellow", reasons

    return "blue", ["各项指标平稳，无异常主诉"]


ADVICE = {
    "red": "立即电话告知家属并指引急诊就诊，同步通知主管医生与护士长；必要时启动紧急联系人流程。",
    "orange": "生成复诊工单，主管医生 24 小时内线上评估，要求 7 日内到专科门诊复查，随访频次加密至每周 1 次。",
    "yellow": "推送健康宣教短信，护士线上回访指导饮食与用药，随访间隔缩短至 14 天，居家监测指标。",
    "blue": "按期常规随访，无需人工干预，继续当前治疗与生活方式管理。",
}

FOLLOW_UP = {"red": "12 小时内", "orange": "7 日内", "yellow": "14 日后", "blue": "按计划"}


def summarize(s: dict, level: str, patient_name: str) -> str:
    parts = [f"{patient_name}本次随访"]
    if s.get("adherence") and s["adherence"] != "未明确":
        parts.append(f"用药{s['adherence']}")
    if s.get("fasting_glucose"):
        parts.append(f"空腹血糖 {s['fasting_glucose']} mmol/L")
    if s.get("postprandial_glucose"):
        parts.append(f"餐后 {s['postprandial_glucose']} mmol/L")
    if s.get("symptoms"):
        parts.append("主诉" + "、".join(s["symptoms"]))
    else:
        parts.append("无特殊不适")
    parts.append(f"复诊{('已落实' if s.get('recheck') == '已预约/已完成' else '未落实') if s.get('recheck') != '未提及' else '情况未提及'}")
    return "，".join(parts) + "。"


LLM_SCHEMA = {
    "adherence": "完全依从|部分依从|自行停药|未明确",
    "fasting_glucose": "数字或null",
    "postprandial_glucose": "数字或null",
    "symptoms": ["症状词组"],
    "adverse_effects": ["药物不良反应"],
    "recheck": "已预约/已完成|未落实|未提及",
    "identity": "本人|家属代接",
    "request": "患者诉求，没有则空字符串",
    "summary": "40字内客观随访小结，不加诊断结论",
    "transferred": "true/false，患者是否表示转往其他医院",
}


def analyze(transcript: list[dict], patient_name: str, use_llm: bool = True) -> dict:
    s = rule_extract(transcript)
    engine = "rule"
    if use_llm and llm.available():
        dialogue = "\n".join(f"{t['speaker']}：{t['text']}" for t in transcript)
        data = llm.chat_json(
            "你是医院随访中心的病历信息抽取助手。只根据通话内容抽取字段，"
            "不得补充通话中没有的信息，不得给出诊断。字段定义：" + str(LLM_SCHEMA),
            {"患者": patient_name, "通话记录": dialogue},
            max_tokens=1200,
        )
        if data:
            for key in ("adherence", "fasting_glucose", "postprandial_glucose", "symptoms",
                        "adverse_effects", "recheck", "identity", "request", "summary", "transferred"):
                if key in data and data[key] not in (None, "", []):
                    s[key] = data[key]
            engine = "llm"

    # 血糖字段统一成数值（取不到则为 None），避免下游比较与前端展示出现字符串
    for key in ("fasting_glucose", "postprandial_glucose"):
        s[key] = _as_float(s.get(key))

    level, reasons = grade_risk(s)
    summary = s.get("summary") or summarize(s, level, patient_name)
    return {
        "structured": s,
        "risk_level": level,
        "risk_reasons": reasons,
        "summary": summary,
        "advice": ADVICE[level],
        "follow_up": FOLLOW_UP[level],
        "engine": engine,
    }
