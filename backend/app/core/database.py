from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.core.config import get_settings


def _normalize_database_url(url: str) -> str:
    # Render/Postgres commonly provides postgres:// or postgresql:// URLs.
    # SQLAlchemy should use the psycopg 3 driver installed by requirements.txt.
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"): ]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"): ]
    return url


settings = get_settings()
database_url = _normalize_database_url(settings.database_url)
connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
engine = create_engine(database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from app.models.document import Document  # noqa: F401
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
