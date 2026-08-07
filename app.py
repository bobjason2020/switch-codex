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
from sy import auth, codex_sync, core, db, probes
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


@asynccontextmanager
async def _lifespan(_: FastAPI):
    auth.ensure_auth()
    codex_sync.ensure_original_snapshot()
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


@app.get("/health")
async def health():
    cfg = core.load_config()
    items = core.load_upstreams()
    active = core.normalize_model(cfg.get("active_model"))
    enabled = [u for u in items if u.get("enabled", True)]
    scoped = [u for u in enabled if core.normalize_model(u.get("model")) == active]
    return {
        "status": "ok",
        "active_model": active,
        "upstreams_enabled": len(enabled),
        "upstreams_in_scope": len(scoped),
        "upstreams_total": len(items),
        "models": core.collect_models(items),
        "codex": codex_sync.status(),
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


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
}


@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
async def spa_fallback(path: str):
    full = "/" + path
    if full in _PAGE_PATHS:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)
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
