from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    source_text: str | None = None
    page_number: int | None = None


class ExtractedField(BaseModel):
    value: Any = None
    evidence: Evidence | None = None


class LineItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class FinancialLineItem(BaseModel):
    label: str
    values: dict[str, float | None] = Field(default_factory=dict)
    evidence: Evidence | None = None


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="allow")
    header: dict[str, Any] = Field(default_factory=dict)
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    periods: list[str] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)
    financial_line_items: list[FinancialLineItem] = Field(default_factory=list)
    raw_text: str | None = None
