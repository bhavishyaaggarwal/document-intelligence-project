import io
import logging
from pathlib import Path
import fitz
from PIL import Image, UnidentifiedImageError
from app.core.config import get_settings
from app.schemas.document import FileValidation

logger = logging.getLogger(__name__)
SUPPORTED_EXTENSIONS = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


class DocumentValidationError(Exception):
    def __init__(self, message: str, code: str, validation: FileValidation | None = None):
        self.message = message
        self.code = code
        self.validation = validation
        super().__init__(message)


def validate_document(filename: str, content: bytes) -> FileValidation:
    settings = get_settings()
    suffix = Path(filename or "").suffix.lower()
    file_type = SUPPORTED_EXTENSIONS.get(suffix, "application/octet-stream")
    if suffix not in SUPPORTED_EXTENSIONS:
        validation = FileValidation(
            file_type=file_type, is_supported=False, is_readable=False,
            page_count=None, status="FAILED", issues=["Unsupported file type."]
        )
        raise DocumentValidationError("Only PDF / JPG / PNG documents are supported.", "UNSUPPORTED_FILE_TYPE", validation)

    if not content:
        validation = FileValidation(
            file_type=file_type, is_supported=True, is_readable=False,
            page_count=None, status="FAILED", issues=["Uploaded file is empty."]
        )
        raise DocumentValidationError("Uploaded file is empty.", "EMPTY_FILE", validation)

    if len(content) > settings.max_upload_mb * 1024 * 1024:
        validation = FileValidation(
            file_type=file_type, is_supported=True, is_readable=False,
            page_count=None, status="FAILED", issues=[f"File exceeds {settings.max_upload_mb} MB limit."]
        )
        raise DocumentValidationError("Uploaded file is too large.", "FILE_TOO_LARGE", validation)

    if suffix == ".pdf":
        try:
            doc = fitz.open(stream=content, filetype="pdf")
            page_count = len(doc)
            if page_count == 0:
                raise ValueError("PDF contains no pages.")
            if page_count > settings.max_pages:
                validation = FileValidation(
                    file_type=file_type, is_supported=True, is_readable=True,
                    page_count=page_count, status="FAILED",
                    issues=[f"Document exceeds the {settings.max_pages}-page limit."]
                )
                raise DocumentValidationError(
                    f"Documents may contain at most {settings.max_pages} pages.",
                    "PAGE_LIMIT_EXCEEDED", validation
                )
            doc.close()
        except DocumentValidationError:
            raise
        except Exception:
            logger.exception("PDF validation failed")
            validation = FileValidation(
                file_type=file_type, is_supported=True, is_readable=False,
                page_count=None, status="FAILED", issues=["PDF is corrupted or unreadable."]
            )
            raise DocumentValidationError("PDF is corrupted or unreadable.", "CORRUPTED_FILE", validation)
    else:
        try:
            image = Image.open(io.BytesIO(content))
            image.verify()
            page_count = 1
        except (UnidentifiedImageError, OSError, ValueError):
            validation = FileValidation(
                file_type=file_type, is_supported=True, is_readable=False,
                page_count=None, status="FAILED", issues=["Image is corrupted or unreadable."]
            )
            raise DocumentValidationError("Image is corrupted or unreadable.", "CORRUPTED_FILE", validation)

    return FileValidation(
        file_type=file_type, is_supported=True, is_readable=True,
        page_count=page_count, status="PASS"
    )
