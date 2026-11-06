"""大模型网关：沿用医疗随访项目的 Agnes 配置，统一为「结构化 JSON 输出」。

设计取舍：
- 比赛演示现场网络不可控，所有调用都有确定性兜底（rule-based），LLM 只做增强。
- 不在日志中输出密钥；密钥优先读环境变量，其次读项目根目录 APIkey.txt。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
MODEL = os.getenv("AGNES_MODEL", "agnes-3.0-flash")


def _key() -> str:
    key = os.getenv("AGNES_API_KEY", "").strip()
    if key:
        return key
    for path in (ROOT / "APIkey.txt", ROOT.parent / "APIkey.txt"):
        if path.exists():
            return path.read_text(encoding="utf-8-sig").strip()
    return ""


def available() -> bool:
    return bool(_key())


def _serverless() -> bool:
    """Vercel / AWS Lambda 等按次计费环境：函数执行时长有硬上限。"""
    return bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


def chat(messages: list[dict], temperature: float = 0.2, max_tokens: int = 2200,
         timeout: float | None = None) -> str | None:
    """带重试的对话调用；现场网络抖动时提高成功率。

    Serverless 环境下把单次等待压到 4 秒且不再重试，避免跨境模型调用
    触发函数执行超时（表现同样是外呼 500）。失败时 nlu.analyze 会自动
    回落到确定性规则引擎，结构化结果、风险分级、工单链路不受影响。
    本地 / ECS 常驻部署维持 60 秒 + 一次重试，可充分使用大模型增强。
    """
    if timeout is None:
        timeout = 4.0 if _serverless() else 60.0
    attempts = 1 if _serverless() else 2
    for _ in range(attempts):
        text = _chat_once(messages, temperature, max_tokens, timeout)
        if text is not None:
            return text
    return None


def _chat_once(messages: list[dict], temperature: float, max_tokens: int, timeout: float) -> str | None:
    key = _key()
    if not key:
        return None
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                BASE_URL.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": MODEL,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
            )
        if resp.status_code != 200:
            return None
        return resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return None


def _loads(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def chat_json(system: str, payload: dict, max_tokens: int = 2400) -> dict | None:
    content = chat(
        [
            {"role": "system", "content": system + "\n只输出一个 JSON 对象，不要 Markdown、不要解释。"},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        max_tokens=max_tokens,
    )
    return _loads(content or "")


def health() -> dict:
    if not available():
        return {"ok": False, "reason": "未配置密钥"}
    try:
        text = chat([{"role": "user", "content": "回答两个字：正常"}], max_tokens=12, timeout=20)
        return {"ok": bool(text), "model": MODEL}
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "reason": str(exc)}
