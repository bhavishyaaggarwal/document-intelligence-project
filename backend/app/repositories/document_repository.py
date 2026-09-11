import json
from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from app.models.document import Document


def upsert_document(db: Session, data: dict) -> Document:
    existing = db.scalar(
        select(Document)
        .where(Document.document_name == data["document_name"])
        .order_by(desc(Document.created_at))
    )
    obj = existing or Document(document_name=data["document_name"])
    obj.document_type = data["document_type"]
    obj.processing_status = data["processing_status"]
    obj.file_validation_json = json.dumps(data["file_validation"])
    obj.extracted_data_json = json.dumps(data["extracted_data"])
    obj.validation_json = json.dumps(data["validation"])
    obj.processing_metadata_json = json.dumps(data["processing_metadata"], default=str)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def get_latest(db: Session, document_name: str) -> Document | None:
    return db.scalar(
        select(Document)
        .where(Document.document_name == document_name)
        .order_by(desc(Document.created_at))
    )


def list_documents(db: Session) -> list[Document]:
    return list(db.scalars(select(Document).order_by(desc(Document.created_at))).all())
