import logging
import json
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.repositories.document_repository import get_latest, list_documents
from app.schemas.document import DocumentResponse, DocumentType
from app.services.document_service import process_document
from app.services.document_validation_service import DocumentValidationError

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
logger = logging.getLogger(__name__)


@router.post("/process", response_model=DocumentResponse)
async def process(file: UploadFile = File(...), document_type: DocumentType = Form(...), db: Session = Depends(get_db)):
    try:
        content = await file.read()
        if not file.filename:
            raise HTTPException(status_code=400, detail={"error": {"code": "MISSING_FILENAME", "message": "A filename is required."}})
        return await process_document(db, file.filename, content, document_type)
    except DocumentValidationError as exc:
        logger.warning("Validation rejected file=%s code=%s", file.filename, exc.code)
        raise HTTPException(status_code=400, detail={"error": {"code": exc.code, "message": exc.message}})
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unhandled document processing failure")
        raise HTTPException(
            status_code=500,
            detail={"error": {"code": "PROCESSING_ERROR", "message": "The document could not be processed."}},
        )


@router.get("", response_model=list[DocumentResponse])
def documents(db: Session = Depends(get_db)):
    results = []
    for obj in list_documents(db):
        results.append(_to_response(obj))
    return results


@router.get("/{document_name}", response_model=DocumentResponse)
def document(document_name: str, db: Session = Depends(get_db)):
    obj = get_latest(db, document_name)
    if not obj:
        raise HTTPException(status_code=404, detail={"error": {"code": "DOCUMENT_NOT_FOUND", "message": "Document not found."}})
    return _to_response(obj)


def _to_response(obj):
    return {
        "document_name": obj.document_name,
        "document_type": obj.document_type,
        "processing_status": obj.processing_status,
        "file_validation": json.loads(obj.file_validation_json),
        "extracted_data": json.loads(obj.extracted_data_json),
        "validation": json.loads(obj.validation_json),
        "processing_metadata": json.loads(obj.processing_metadata_json),
    }
