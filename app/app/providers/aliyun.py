"""阿里云语音服务（语音通知 / 语音双呼）适配器。

生产路径：创建真实外呼 → 阿里云回执 → 通话录音与 ASR 文本回传 → 结构化。
演示路径：未配置 AccessKey 时自动降级为本地模拟器，保证答辩现场可跑通全流程。

接口：dyvmsapi.aliyuncs.com，OpenAPI V3 签名（ACS3-HMAC-SHA256）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

ENDPOINT = "https://dyvmsapi.aliyuncs.com"
HOST = "dyvmsapi.aliyuncs.com"
VERSION = "2017-05-25"


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v.strip()
    return default


def configured() -> bool:
    return bool(_env("ALIYUN_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_ID")
                and _env("ALIYUN_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _percent(value: str) -> str:
    return quote(str(value), safe="-_.~")


def call_api(action: str, params: dict) -> dict:
    """调用阿里云 RPC 风格 OpenAPI（V3 签名）。"""
    ak_id = _env("ALIYUN_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_ID")
    ak_secret = _env("ALIYUN_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    if not ak_id or not ak_secret:
        raise RuntimeError("未配置阿里云 AccessKey，无法发起真实外呼")

    business = {"Action": action, "Version": VERSION, "Format": "JSON", "RegionId": "cn-hangzhou"}
    business.update({k: str(v) for k, v in params.items() if v not in (None, "")})

    body = json.dumps({k: str(v) for k, v in params.items() if v not in (None, "")},
                      ensure_ascii=False).encode() if params else b"{}"
    content_hash = _sha256_hex(body)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = uuid.uuid4().hex

    canonical_query = "&".join(f"{_percent(k)}={_percent(v)}" for k, v in sorted(business.items()))
    signed = {
        "content-type": "application/json",
        "host": HOST,
        "x-acs-content-sha256": content_hash,
        "x-acs-date": date,
        "x-acs-signature-nonce": nonce,
    }
    signed_names = ";".join(sorted(signed))
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(signed.items()))

    canonical_request = "\n".join([
        "POST", "/", canonical_query, canonical_headers, signed_names, content_hash,
    ])
    string_to_sign = "ACS3-HMAC-SHA256\n" + _sha256_hex(canonical_request.encode())
    signature = hmac.new(ak_secret.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (f"ACS3-HMAC-SHA256 Credential={ak_id},SignedHeaders={signed_names},"
                     f"Signature={signature}")

    headers = {
        "Authorization": authorization,
        "Content-Type": "application/json",
        "Host": HOST,
        "x-acs-content-sha256": content_hash,
        "x-acs-date": date,
        "x-acs-signature-nonce": nonce,
    }
    url = f"{ENDPOINT}/?{canonical_query}"
    with httpx.Client(timeout=20) as client:
        resp = client.post(url, headers=headers, content=body)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        raise RuntimeError(f"阿里云返回 {resp.status_code}: {data}")
    return data


def single_call_tts(called: str, show_number: str, tts_code: str, tts_param: str = "",
                    play_times: int = 2, volume: int = 60) -> dict:
    """语音通知外呼（SingleCallByTts），用于复查提醒等单向通知场景。"""
    return call_api("SingleCallByTts", {
        "CalledNumber": called, "CalledShowNumber": show_number, "TtsCode": tts_code,
        "TtsParam": tts_param or "{}", "PlayTimes": play_times, "Volume": volume,
        "OutId": uuid.uuid4().hex[:16],
    })


def single_call_voice(called: str, show_number: str, voice_code: str) -> dict:
    """语音文件外呼（SingleCallByVoice）。"""
    return call_api("SingleCallByVoice", {
        "CalledNumber": called, "CalledShowNumber": show_number,
        "VoiceCode": voice_code, "PlayTimes": 2, "Volume": 60,
    })


def query_call(call_id: str) -> dict:
    return call_api("QueryCallDetailByCallId", {
        "CallId": call_id, "QueryDate": int(datetime.now().timestamp() * 1000)})
