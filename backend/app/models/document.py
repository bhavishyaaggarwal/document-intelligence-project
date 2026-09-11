from datetime import datetime, timezone
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_name: Mapped[str] = mapped_column(String(255), index=True)
    document_type: Mapped[str] = mapped_column(String(40))
    processing_status: Mapped[str] = mapped_column(String(20))
    file_validation_json: Mapped[str] = mapped_column(Text)
    extracted_data_json: Mapped[str] = mapped_column(Text)
    validation_json: Mapped[str] = mapped_column(Text)
    processing_metadata_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
