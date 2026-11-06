"""SQLite 引擎与会话（融合版统一使用 SQLite，去掉原外呼平台对 PostgreSQL 的强依赖）。

Serverless 兼容（Vercel / AWS Lambda）：
    代码层（/var/task）挂载为**只读**，直接对随包发布的 data/yizhihu.db 执行
    commit 会抛 "attempt to write a readonly database"，表现为所有 GET 正常、
    所有 POST/PUT（发起外呼、新建患者、保存设置）一律 500。
    首次导入时把预置演示库复制到 /tmp —— Lambda 唯一可写目录 —— 之后读写
    都落在 /tmp 副本上。本地开发目录可写时保持原路径，行为完全不变。

    /tmp 副本的生命周期跟随函数实例：同一实例内外呼结果可正常回读；
    实例冷启动或切换后会重新从随包快照初始化（演示数据自动复位），
    这恰好适合评审反复演示，不会把演示库改脏。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_FILE = DATA_DIR / "yizhihu.db"

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # 只读代码层里 data/ 已存在，mkdir 本身无害；这里只是防止极端情况中断导入
    pass


def _readonly_bundle() -> bool:
    """判断当前运行环境是否属于「代码包只读」的 Serverless。"""
    if os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return True
    try:
        return not os.access(DATA_DIR, os.W_OK)
    except OSError:
        return False


if _readonly_bundle():
    runtime_db = Path(tempfile.gettempdir()) / "yizhihu-runtime.db"
    if not runtime_db.exists() and DB_FILE.exists():
        try:
            shutil.copy2(DB_FILE, runtime_db)
        except OSError:
            # 复制失败时仍保持原路径，至少读接口可用，不会让整个应用启动失败
            runtime_db = DB_FILE
    DB_FILE = runtime_db

engine = create_engine(
    f"sqlite:///{DB_FILE}",
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _fk_on(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()
