from __future__ import annotations

import importlib.metadata
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.body_limit import BodyLimitMiddleware
from app.api.errors import register_exception_handlers
from app.api.public import router as public_router
from app.api.router import router
from app.config.settings import settings
from app.db.engine import close_db, init_db
from app.logging import get_logger, setup_logging
from app.mcp import mcp_lifespan, mount_mcp

DISTRIBUTION_NAME = "alexandria"
FALLBACK_VERSION = "0.1.0"
GRACEFUL_SHUTDOWN_SECONDS = 20
WEB_STATIC = Path(__file__).parent.parent / "web" / "static"
ADMIN_DIST = Path(__file__).parent.parent / "admin" / "dist"

logger = get_logger("app.main")
request_logger = get_logger("app.request")


def application_version() -> str:
    try:
        return importlib.metadata.version(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return FALLBACK_VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.logging.level)
    logger.info("Starting Alexandria", extra={"extra_data": {"level": settings.logging.level}})
    await init_db()
    logger.info("Database initialized")
    async with mcp_lifespan():
        yield
    logger.info("Shutting down Alexandria")
    await close_db()


app = FastAPI(
    title="Alexandria",
    description="A lightweight skill/capability registry built around MCP",
    version=application_version(),
    lifespan=lifespan,
)

app.add_middleware(BodyLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors.allowed_origins,
    allow_credentials=settings.cors.allow_credentials,
    allow_methods=settings.cors.allow_methods,
    allow_headers=settings.cors.allow_headers,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    import time

    start = time.time()
    response = await call_next(request)
    elapsed_ms = round((time.time() - start) * 1000, 2)
    request_logger.info(
        f"{request.method} {request.url.path} -> {response.status_code}",
        extra={
            "extra_data": {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "elapsed_ms": elapsed_ms,
            }
        },
    )
    return response


register_exception_handlers(app)


app.include_router(router)
mount_mcp(app)
app.include_router(public_router)

if WEB_STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_STATIC)), name="static")

if ADMIN_DIST.exists():
    app.mount("/admin", StaticFiles(directory=str(ADMIN_DIST), html=True), name="admin")


def run():
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    run()
