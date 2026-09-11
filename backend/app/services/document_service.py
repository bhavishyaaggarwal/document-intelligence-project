import json
import logging
import time
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.repositories.document_repository import upsert_document
from app.schemas.document import DocumentType
from app.services.document_validation_service import validate_document
from app.services.ocr_service import extract_text
from app.services.extraction_service import extract
from app.services.financial_validation_service import validate

logger = logging.getLogger(__name__)


async def process_document(db: Session, filename: str, content: bytes, document_type: DocumentType):
    started = time.perf_counter()
    logger.info("Processing request received: name=%s type=%s", filename, document_type.value)

    file_validation = validate_document(filename, content)
    logger.info("File validation passed: name=%s pages=%s", filename, file_validation.page_count)

    ocr = extract_text(content, filename)
    logger.info("Text extraction complete: name=%s ocr_used=%s", filename, ocr.used_ocr)

    extracted, extraction_method = await extract(document_type, ocr)
    logger.info("Structured extraction complete: name=%s method=%s", filename, extraction_method)

    validation = validate(document_type, extracted)
    logger.info("Financial validation complete: name=%s status=%s", filename, validation.overall_status)

    if validation.overall_status == "FAIL":
        # The file was successfully read and extracted; a failed financial check is
        # a document-quality/extraction warning, not a transport or processing failure.
        status = "COMPLETED_WITH_WARNINGS"
    elif validation.overall_status in {"PASS", "NOT_APPLICABLE"}:
        status = "COMPLETED"
    else:
        status = "FAILED"
    metadata = {
        "ocr_used": ocr.used_ocr,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "processing_time_ms": int((time.perf_counter() - started) * 1000),
        "extraction_method": extraction_method,
        "pages_processed": len(ocr.pages),
        "build_version": "v11-gemini-vision",
    }
    result = {
        "document_name": filename,
        "document_type": document_type.value,
        "processing_status": status,
        "file_validation": file_validation.model_dump(),
        "extracted_data": extracted,
        "validation": validation.model_dump(),
        "processing_metadata": metadata,
    }
    upsert_document(db, result)
    logger.info("Processing persisted: name=%s duration_ms=%s", filename, metadata["processing_time_ms"])
    return result
