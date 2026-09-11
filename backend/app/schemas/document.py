from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    invoice = "invoice"
    balance_sheet = "balance_sheet"
    profit_and_loss = "profit_and_loss"
    cash_flow_statement = "cash_flow_statement"


class FileValidation(BaseModel):
    file_type: str
    is_supported: bool
    is_readable: bool
    page_count: int | None = None
    status: str
    issues: list[str] = Field(default_factory=list)


class ValidationCheck(BaseModel):
    name: str
    formula: str
    operands: dict[str, Any] = Field(default_factory=dict)
    calculated_value: float | None = None
    reported_value: float | None = None
    variance: float | None = None
    status: str
    period: str | None = None


class ValidationResult(BaseModel):
    checks: list[ValidationCheck] = Field(default_factory=list)
    overall_status: str
    issues: list[str] = Field(default_factory=list)


class ProcessingMetadata(BaseModel):
    ocr_used: bool
    processed_at: datetime
    processing_time_ms: int
    extraction_method: str
    pages_processed: int
    build_version: str | None = None


class DocumentResponse(BaseModel):
    document_name: str
    document_type: DocumentType
    processing_status: str
    overall_confidence: float | None = None
    file_validation: FileValidation
    extracted_data: dict[str, Any]
    validation: ValidationResult
    processing_metadata: ProcessingMetadata


class ErrorResponse(BaseModel):
    error: dict[str, str]
