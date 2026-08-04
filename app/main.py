"""FastAPI application entry point."""

# Force IPv4-only resolution to prevent slow IPv6 connection hangs on macOS local networks
import socket
orig_getaddrinfo = socket.getaddrinfo

def getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

socket.getaddrinfo = getaddrinfo_ipv4_only

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
from app.seed import seed_database

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: connect to MongoDB on startup, close on shutdown."""
    await init_db()
    await seed_database()
    yield
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
app.include_router(router)


# ── Health Check ──────────────────────────────────────────────────────────────


@app.get("/health", tags=["Health"])
async def health_check():
    """Simple health check endpoint."""
    return {"status": "healthy", "database": "MongoDB Atlas", "version": "1.0.0"}
