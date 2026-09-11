import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.api.routes.documents import router as document_router
from app.core.config import get_settings
from app.core.database import init_db
from app.core.logging import configure_logging

configure_logging()
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Application started")
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.1.0",
    description="Document extraction, financial validation and persistence API.",
    lifespan=lifespan,
)
origins = [x.strip() for x in settings.cors_origins.split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins if origins != ["*"] else ["*"],
    allow_credentials=origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(document_router)
app.mount('/static', StaticFiles(directory=Path(__file__).resolve().parents[2] / 'frontend' / 'static'), name='static')


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "service": settings.app_name, "build_version": "v11.3-gemini-vision"}


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(Path(__file__).resolve().parents[2] / "frontend" / "templates" / "dashboard.html")
