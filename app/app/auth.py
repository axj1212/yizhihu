"""竞赛评审登录门禁：为评委提供固定「账号 + 密码」入口。

设计要点（针对 Vercel / Serverless 的 yizhihu 正式作品）：
- 只用 Python 标准库，不引入新依赖，不影响 requirements.txt。
- 用 HTTP middleware 实现而不是注册路由，因为 main.py 末尾有
  `/{path:path}` SPA catch-all，注册 /login 路由会被它抢先匹配。
- 登录态是 HMAC 签名的 Cookie，不依赖数据库或服务端会话存储；
  Serverless 多实例 / 冷启动都能校验，无需共享 Redis 或 /tmp。
- 账号、密码、签名密钥都可用环境变量覆盖；默认值即竞赛提交材料中的账号密码。
- 设 REVIEW_GATE=0 可临时关闭（例如本地免密调试），线上默认开启。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from urllib.parse import parse_qs

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE_NAME = "yzh_review_session"
REVIEW_USER = os.getenv("REVIEW_USER", "reviewer")
REVIEW_PASSWORD = os.getenv("REVIEW_PASSWORD", "Yizhihu2026")
REVIEW_SECRET = os.getenv("REVIEW_SECRET", "yizhihu-judge-demo-2026")
SESSION_TTL = 7 * 24 * 3600
GATE_ON = os.getenv("REVIEW_GATE", "1") not in ("0", "false", "False", "off")

# 平台健康检查 / 图标放行；静态资源放行，但 HTML 页面必须登录，
# 否则 /static/index.html 会绕过登录页直接打开工作台外壳。
PUBLIC_EXACT = {"/api/health", "/favicon.ico", "/favicon.svg"}
PUBLIC_PREFIX = ("/static/",)

LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>医智呼 · 评审登录</title>
<style>
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
 background:#f5f8f7;color:#1f2f2e;
 font-family:"PingFang SC","Microsoft YaHei","Segoe UI",system-ui,-apple-system,sans-serif}
.card{width:min(400px,92vw);background:#fff;border:1px solid #dce7e5;border-radius:12px;
 padding:32px 32px 26px;box-shadow:0 2px 10px rgba(31,47,46,.06)}
.logo{width:46px;height:46px;border-radius:12px;background:#2f9e90;color:#fff;
 display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;margin-bottom:16px}
h1{font-size:20px;line-height:28px;font-weight:700;margin:0 0 6px;color:#1f2f2e}
.sub{color:#455a57;font-size:13px;line-height:20px;margin:0 0 20px}
label{display:block;font-size:13px;font-weight:600;margin-bottom:14px;color:#1f2f2e}
input{width:100%;margin-top:6px;padding:9px 12px;border:1px solid #b9d3cf;border-radius:8px;
 font-size:14px;color:#1f2f2e;background:#fff;outline:none;transition:border-color .2s,box-shadow .2s}
input:focus{border-color:#2f9e90;box-shadow:0 0 0 3px rgba(47,158,144,.12)}
button{width:100%;padding:11px;border:1px solid #2f9e90;border-radius:8px;background:#2f9e90;color:#fff;
 font-size:15px;font-weight:600;cursor:pointer;margin-top:4px;transition:background-color .2s,border-color .2s}
button:hover{background:#248578;border-color:#248578}
.err{display:none;background:#fdecec;color:#b42318;border:1px solid #f5c2c2;
 border-radius:8px;padding:9px 12px;font-size:13px;margin-bottom:16px}
.err.show{display:block}
.note{color:#7f9491;font-size:12px;line-height:18px;margin:16px 0 0;text-align:center}
</style></head><body>
<div class="card">
 <div class="logo">医</div>
 <h1>医智呼 · AI 随访外呼系统</h1>
 <p class="sub">评审专用入口 · 请使用参赛材料中的账号登录</p>
 <div class="err __ERRCLS__">__ERR__</div>
 <form method="post" action="/login">
  <label>账号<input name="username" autocomplete="username" required autofocus></label>
  <label>密码<input name="password" type="password" autocomplete="current-password" required></label>
  <button type="submit">登 录</button>
 </form>
 <p class="note">登录后可查看工作台、病种随访方案、患者档案、外呼中心与风险工单。<br>演示数据均为虚构脱敏数据。</p>
</div></body></html>"""


def _sign(payload: str) -> str:
    return hmac.new(REVIEW_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _make_token(user: str) -> str:
    payload = f"{user}|{int(time.time()) + SESSION_TTL}"
    raw = base64.urlsafe_b64encode(payload.encode()).decode()
    return raw + "." + _sign(payload)


def _verify_token(token: str) -> bool:
    if not token:
        return False
    try:
        raw, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(raw.encode()).decode()
        if not hmac.compare_digest(sig, _sign(payload)):
            return False
        user, exp = payload.split("|", 1)
        return user == REVIEW_USER and int(exp) > int(time.time())
    except Exception:
        return False


def _render(error: str = "") -> HTMLResponse:
    html = LOGIN_PAGE.replace("__ERR__", error).replace(
        "__ERRCLS__", "show" if error else ""
    )
    return HTMLResponse(html, status_code=401 if error else 200)


def _is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    if path.startswith(PUBLIC_PREFIX):
        # 静态 js/css 可以放行；静态 HTML 页面一律要求登录，防止绕过门禁。
        return not path.lower().endswith((".html", ".htm"))
    return False


def install_auth(app) -> None:
    """挂到 FastAPI 应用上：未登录一律先到登录页。

    放在 CORSMiddleware 之后调用，使本中间件位于最外层，先于业务路由执行。
    """
    if not GATE_ON:
        return

    @app.middleware("http")
    async def review_gate(request, call_next):
        path = request.url.path

        if path == "/login":
            if request.method == "GET":
                return _render()
            if request.method == "POST":
                body = (await request.body()).decode("utf-8", "ignore")
                form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
                user = form.get("username", "").strip()
                pwd = form.get("password", "")
                if user == REVIEW_USER and pwd == REVIEW_PASSWORD:
                    resp = RedirectResponse("/", status_code=303)
                    resp.set_cookie(
                        COOKIE_NAME, _make_token(REVIEW_USER),
                        max_age=SESSION_TTL, httponly=True, samesite="lax",
                        secure=(request.url.scheme == "https"), path="/",
                    )
                    return resp
                return _render("账号或密码不正确，请重试。")

        if path == "/logout":
            resp = RedirectResponse("/login", status_code=303)
            resp.delete_cookie(COOKIE_NAME, path="/")
            return resp

        if _is_public(path) or _verify_token(request.cookies.get(COOKIE_NAME, "")):
            return await call_next(request)

        if path.startswith("/api/"):
            return JSONResponse({"detail": "请先登录评审账号。"}, status_code=401)
        return RedirectResponse("/login", status_code=302)
