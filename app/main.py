"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import init_db, close_db
from app.routes import router
from app.auth_routes import router as auth_router
from app.partition_routes import router as partition_router
from app.admin_routes import router as admin_router
from app.payment_routes import router as payment_router
from app.subscription_routes import router as subscription_router
from app.workspace_routes import router as workspace_router
from app.execution_routes import router as execution_router
from app.terminal_routes import router as terminal_router
from app.preview_routes import router as preview_router
from app.contact_routes import router as contact_router
from app.seed import seed_database

import asyncio
import logging

settings = get_settings()


async def _billing_expiry_worker_loop(interval_seconds: int):
    """Background periodic worker for processing expired subscriptions and grace periods."""
    worker_logger = logging.getLogger("billing_expiry_worker")
    from app.billing_service import process_expired_subscriptions

    # Initial small delay after startup to let DB initialize
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        return

    while True:
        try:
            worker_logger.info("Running scheduled billing expiration check...")
            results = await process_expired_subscriptions()
            worker_logger.info(f"Scheduled billing expiration check completed: {results}")
        except asyncio.CancelledError:
            worker_logger.info("Billing expiry worker task cancelled.")
            break
        except Exception as exc:
            worker_logger.error(f"Unhandled error in billing expiry worker loop: {exc}", exc_info=True)

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: connect to MongoDB on startup, start background tasks, close on shutdown."""
    app_logger = logging.getLogger("app")
    
    if settings.APP_ENV.lower() == "production" and settings.JWT_SECRET_KEY == "supersecretkeyforlocaldevelopmentfilestoreapp":
        app_logger.critical("SECURITY WARNING: Using default development JWT_SECRET_KEY in production mode!")
        
    await init_db()
    await seed_database()

    # Start automated background expiry scheduler
    expiry_task = None
    if settings.BILLING_EXPIRY_WORKER_ENABLED:
        app_logger.info(f"Starting billing expiry worker (interval: {settings.BILLING_EXPIRY_INTERVAL_SECONDS}s)")
        expiry_task = asyncio.create_task(_billing_expiry_worker_loop(settings.BILLING_EXPIRY_INTERVAL_SECONDS))

    yield

    if expiry_task:
        expiry_task.cancel()
        try:
            await expiry_task
        except (asyncio.CancelledError, Exception):
            pass

    await close_db()


app = FastAPI(
    title="File Manager API",
    description="Backend API for the File Manager application — powered by MongoDB",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(partition_router)
app.include_router(admin_router)
app.include_router(payment_router)
app.include_router(subscription_router)
app.include_router(workspace_router)
app.include_router(execution_router)
app.include_router(terminal_router)
app.include_router(preview_router)
app.include_router(contact_router)
app.include_router(router)


# ── Health Check ──────────────────────────────────────────────────────────────


@app.get("/health", tags=["Health"])
async def health_check():
    """Simple health check endpoint."""
    return {"status": "healthy", "database": "MongoDB Atlas", "version": "1.0.1"}
