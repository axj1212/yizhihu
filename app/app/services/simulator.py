"""外呼对话模拟器（演示模式）。

真实外呼由阿里云语音通道完成：ASR 把患者语音转文字回传，
本模块用来在没有线路/白名单的环境下复现同一条数据链路——
按医生编排的阶段问题逐条提问，按患者人设生成口语化回答，
产出与真实通话同构的 transcript，再交给大模型做结构化抽取。
"""
from __future__ import annotations

import random

PERSONAS = {
    "稳定": {
        "label": "依从性好、指标平稳",
        "identity": "是我，你说吧。",
        "adherence": "药都按时吃的，一天都没落下。",
        "glucose": "空腹大概6.2，餐后两小时8.1，都还稳。",
        "foot": "脚挺好的，没麻也没破。",
        "diet": "主食减了，每天晚上出去走半个小时。",
        "adverse": "没什么不舒服，肚子也不胀。",
        "complication": "眼睛看得清，腿也不肿。",
        "recheck": "复查已经约上了，下周去。",
        "wound": "伤口长得挺好，已经结痂了。",
        "drainage": "引流管拔了，这两天没什么水了。",
        "arm": "胳膊能抬起来了，就是还有点紧。",
        "breath": "咳嗽好多了，痰也白了。",
        "nerve": "腿不怎么疼了，能走一公里。",
        "treatment": "治疗按时在做，就是有点没力气。",
        "close": "好的，谢谢你们还惦记着。",
    },
    "漏服": {
        "label": "偶尔漏服、指标轻度波动",
        "identity": "是我。",
        "adherence": "大部分时间都吃，就是晚上睡得早，一周会忘记两三次。",
        "glucose": "空腹7.6左右，餐后有时候到11。",
        "foot": "脚有点发凉，没破皮。",
        "diet": "主食没怎么控制住，爱吃面食。",
        "adverse": "吃了二甲双胍有时候肚子胀。",
        "complication": "别的没什么。",
        "recheck": "还没约，最近忙。",
        "wound": "伤口还行，就是有点痒。",
        "drainage": "引流管还在，一天大概二十毫升。",
        "arm": "胳膊抬到肩膀就疼。",
        "breath": "还有点咳，白痰。",
        "nerve": "腿还是有点麻。",
        "treatment": "化疗推迟了一次，白细胞低。",
        "close": "行，我记着了。",
    },
    "血糖偏高": {
        "label": "血糖持续偏高、饮食失控",
        "identity": "我是。",
        "adherence": "药是吃了，就是有时候想起来才吃。",
        "glucose": "空腹一直在10.5上下，餐后经常到14，最近饭局多，主食没控制住。",
        "foot": "脚没破，就是有点麻。",
        "diet": "家里来客人，米饭和酒都没少。",
        "adverse": "没有。",
        "complication": "这两天口干，喝水多，夜里起夜两三次。",
        "recheck": "还没去复查。",
        "wound": "伤口没事。",
        "drainage": "引流不多。",
        "arm": "胳膊还行。",
        "breath": "痰有点黄。",
        "nerve": "腰还是酸。",
        "treatment": "治疗正常。",
        "close": "好，我尽量控制。",
    },
    "足部麻木": {
        "label": "周围神经病变加重",
        "identity": "是我，你说。",
        "adherence": "甲钴胺有时候忘了吃，降糖药在吃。",
        "glucose": "空腹9.8，餐后13.5。",
        "foot": "两只脚趾天天发麻，夜里疼得睡不着，走路像踩棉花。",
        "diet": "吃得不多。",
        "adverse": "没有。",
        "complication": "眼睛有点花。",
        "recheck": "肌电图还没做。",
        "wound": "伤口没事。",
        "drainage": "没有引流了。",
        "arm": "胳膊能抬。",
        "breath": "不喘。",
        "nerve": "腿麻得厉害，走路发飘。",
        "treatment": "治疗在继续。",
        "close": "好的，我尽快去门诊。",
    },
    "自行停药": {
        "label": "自行停用降糖药",
        "identity": "是我。",
        "adherence": "我感觉没什么不舒服，胰岛素停了快一周了，药也吃得少。",
        "glucose": "没怎么测，前天早上测是13。",
        "foot": "脚没事。",
        "diet": "正常吃。",
        "adverse": "没有。",
        "complication": "就是有点乏力、口干。",
        "recheck": "没去。",
        "wound": "伤口好着呢。",
        "drainage": "没引流。",
        "arm": "胳膊没事。",
        "breath": "不咳嗽。",
        "nerve": "腿不疼。",
        "treatment": "治疗停了。",
        "close": "那我先把药吃上。",
    },
    "低血糖": {
        "label": "反复低血糖发作",
        "identity": "是我。",
        "adherence": "药和胰岛素都按时用。",
        "glucose": "空腹6.8，但是昨天上午突然浑身发软、出冷汗、手抖，吃了两块糖才缓过来，这周有两三次了。",
        "foot": "脚没事。",
        "diet": "有时候忙起来就漏一顿饭。",
        "adverse": "没有。",
        "complication": "没有别的。",
        "recheck": "还没复查。",
        "wound": "伤口没事。",
        "drainage": "没有。",
        "arm": "胳膊正常。",
        "breath": "不喘。",
        "nerve": "腿不麻。",
        "treatment": "治疗照常。",
        "close": "好，我随身带点糖。",
    },
    "足部破溃": {
        "label": "糖尿病足早期破溃",
        "identity": "是我。",
        "adherence": "药在吃。",
        "glucose": "空腹10.2，餐后12.8。",
        "foot": "右脚大脚趾边上磨破了一块，一周了还没长好，有点渗水。",
        "diet": "吃得还行。",
        "adverse": "没有。",
        "complication": "脚有点肿。",
        "recheck": "没约。",
        "wound": "伤口渗水，边上有点发白。",
        "drainage": "没有引流。",
        "arm": "胳膊正常。",
        "breath": "不喘。",
        "nerve": "腿麻。",
        "treatment": "治疗正常。",
        "close": "好，我明天就去。",
    },
    "酮症": {
        "label": "疑似酮症酸中毒（红色）",
        "identity": "是我……（声音虚弱）",
        "adherence": "这两天吃不下饭，药也停了。",
        "glucose": "早上测17.2，一直口渴，还恶心吐了两次。",
        "foot": "脚没事。",
        "diet": "吃不下。",
        "adverse": "恶心、没力气。",
        "complication": "头晕，浑身没劲。",
        "recheck": "没去。",
        "wound": "伤口没事。",
        "drainage": "没有。",
        "arm": "胳膊没劲。",
        "breath": "有点喘。",
        "nerve": "腿软。",
        "treatment": "停了。",
        "close": "……好。",
    },
    "转院": {
        "label": "已转往其他医院（脱访）",
        "identity": "是我，不过我们已经转到省立医院接着治了。",
        "adherence": "那边的医生重新开了药，这边开的就没再吃了。",
        "glucose": "转过去之后测过一次，7.8。",
        "foot": "脚没事。",
        "diet": "正常吃。",
        "adverse": "没有。",
        "complication": "没有别的。",
        "recheck": "在省立医院复查。",
        "wound": "伤口那边医生看过了。",
        "drainage": "引流拔了。",
        "arm": "胳膊还行。",
        "breath": "不喘。",
        "nerve": "腿不麻。",
        "treatment": "转到省立医院继续治疗。",
        "close": "以后就不用这边随访了，谢谢。",
    },
    "家属代接": {
        "label": "家属代接",
        "identity": "我是他女儿，我爸耳朵不好，我替他答。",
        "adherence": "二甲双胍在吃，血糖空腹七左右。",
        "glucose": "空腹7.1，餐后9.6。",
        "foot": "脚没破，就是有点凉。",
        "diet": "吃得清淡。",
        "adverse": "没有。",
        "complication": "这两天有点咳嗽。",
        "recheck": "复查我陪他去。",
        "wound": "伤口好得差不多了。",
        "drainage": "引流拔了。",
        "arm": "胳膊我帮他练。",
        "breath": "有点咳。",
        "nerve": "腿还麻。",
        "treatment": "治疗我盯着。",
        "close": "好的，谢谢医生。",
    },
}

PERSONA_ORDER = ["稳定", "漏服", "血糖偏高", "足部麻木", "自行停药", "低血糖", "足部破溃", "酮症", "家属代接"]


def pick_persona(patient, stage_key: str, index: int = 0) -> str:
    """演示用：按患者风险分层与序号稳定地分配人设，保证每次演示一致。"""
    if "高危" in (patient.risk_group or ""):
        pool = ["足部麻木", "血糖偏高", "低血糖", "稳定", "足部破溃", "酮症"]
    else:
        pool = ["稳定", "漏服", "稳定", "血糖偏高", "稳定", "家属代接"]
    return pool[index % len(pool)]


def _answer(persona: dict, purpose: str, text: str) -> str:
    p = purpose or ""
    t = text or ""
    if "身份" in p:
        return persona["identity"]
    if "用药" in p or "依从" in p:
        return persona["adherence"]
    if "血糖" in p:
        return persona["glucose"]
    if "足部" in p or "足" in t:
        return persona["foot"]
    if "饮食" in p or "运动" in p or "生活" in p:
        return persona["diet"]
    if "不良" in p:
        return persona["adverse"]
    if "复诊" in p or "复查" in p:
        return persona["recheck"]
    if "切口" in p or "伤口" in p:
        return persona["wound"]
    if "引流" in p:
        return persona["drainage"]
    if "患肢" in p:
        return persona["arm"]
    if "气促" in p or "呼吸" in p or "咳嗽" in p:
        return persona["breath"]
    if "神经" in p or "马尾" in p:
        return persona["nerve"]
    if "治疗" in p:
        return persona["treatment"]
    if "并发症" in p or "症状" in p:
        return persona["complication"]
    return "还行，没什么特别的。"


def simulate(patient, stage, persona_key: str | None = None) -> dict:
    """生成一次完整通话的转写文本。返回 transcript 与通话元信息。"""
    key = persona_key or "稳定"
    persona = PERSONAS.get(key, PERSONAS["稳定"])
    questions = stage.questions or []
    if not questions:
        questions = [{"text": f"您好，{patient.name}，了解一下出院后的恢复情况。", "purpose": "身份确认"}]

    turns: list[dict] = []
    second = 2
    turns.append({"speaker": "AI", "text": questions[0]["text"], "second": second})
    second += 6
    for idx, q in enumerate(questions[1:]):
        turns.append({"speaker": "患者", "text": _answer(persona, q.get("purpose", ""), q.get("text", "")), "second": second})
        second += 9
        # 分支追问：异常人设会多一轮，体现「多级追问」
        if key in ("血糖偏高", "足部麻木", "足部破溃", "酮症") and idx == 2:
            follow = {
                "血糖偏高": "那这两天有没有恶心、呕吐、肚子疼，或者呼吸有烂苹果味？",
                "足部麻木": "是两只脚都麻还是单侧？晚上会不会更明显？",
                "足部破溃": "破的地方有没有发烧、发黑或者味道很重？",
                "酮症": "有没有意识模糊、叫不太应的情况？身边有人照顾吗？",
            }[key]
            turns.append({"speaker": "AI", "text": follow, "second": second})
            second += 6
            follow_a = {
                "血糖偏高": "有点恶心，没吐。",
                "足部麻木": "两只脚都麻，晚上更厉害。",
                "足部破溃": "没发烧，就是有点味。",
                "酮症": "有点迷糊，我老伴在旁边。",
            }[key]
            turns.append({"speaker": "患者", "text": follow_a, "second": second})
            second += 8
        turns.append({"speaker": "AI", "text": q["text"], "second": second})
        second += 7

    turns.append({"speaker": "患者", "text": _answer(persona, questions[-1].get("purpose", ""), ""), "second": second})
    second += 6
    turns.append({"speaker": "AI", "text": "好的，都记下了。如果出现血糖骤升、脚上破溃、胸痛这些情况，请直接来急诊。祝您早日康复，再见。", "second": second})
    turns.append({"speaker": "患者", "text": persona["close"], "second": second + 4})

    return {
        "transcript": turns,
        "persona": key,
        "persona_label": persona["label"],
        "duration": second + 8,
    }
