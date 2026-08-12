#!/usr/bin/env python3
"""Switch-codex — OpenAI Responses 风格的多上游 API 路由调度台。

入口模块只负责组装 FastAPI：注册 admin/proxy 路由、静态文件与后台任务。
业务逻辑拆在 sy/ 包内（core / logbook / probes / auth / proxy / api / db / codex_sync）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import sy
from sy import auth, core, db, migrate_deepseek, migrate_grok, probes
from sy.api import router as api_router
from sy.const import STATIC
from sy.proxy import router as proxy_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("switchyard.app")

# 初始化 SQLite 并导入旧 JSON/JSONL 数据、迁移旧库文件。
db.init_db()
db.migrate_legacy_data()
migrate_deepseek.migrate()
migrate_grok.migrate()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    auth.ensure_auth()
    auth.restore_admin_sessions()
    probes._start_background_loops()
    yield


app = FastAPI(
    title=f"{sy.PROJECT_NAME} Router",
    version=sy.__version__,
    lifespan=_lifespan,
)

app.include_router(api_router)
app.include_router(proxy_router)

if (STATIC / "css").exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-XSS-Protection", "0")
    path = request.url.path
    if path.startswith("/api") or path.startswith("/v1"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.get("/health")
async def health():
    cfg = core.load_config()
    return {
        "status": "ok",
        "active_model": core.normalize_model(cfg.get("active_model")),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# SPA 路径路由兜底：/logs、/settings/pricing 等路径刷新时直接返回管理面板。
_PAGE_PATHS = {
    "/",
    "/logs",
    "/history",
    "/upstreams",
    "/errors",
    "/settings",
    "/settings/model",
    "/settings/pricing",
    "/settings/newapi",
    "/settings/public",
}


@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
async def spa_fallback(path: str):
    full = "/" + path
    if full in _PAGE_PATHS:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="Not Found")



def main():
    import uvicorn

    cfg = core.load_config()
    uvicorn.run(
        "app:app",
        host=cfg.get("host", "127.0.0.1"),
        port=int(cfg.get("port", 4100)),
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
