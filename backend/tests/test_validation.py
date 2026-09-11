from app.schemas.document import DocumentType
from app.services.financial_validation_service import validate


def test_invoice_math_and_cash_change():
    data = {
        "fields": {
            "subtotal": {"value": 100},
            "tax_amount": {"value": 10},
            "discount": {"value": 5},
            "total_amount": {"value": 105},
            "cash_paid": {"value": 110},
            "change": {"value": 5},
        },
        "line_items": [
            {"description": "A", "quantity": 2, "unit_price": 50, "amount": 100}
        ],
    }
    result = validate(DocumentType.invoice, data)
    assert all(c.status == "PASS" for c in result.checks)


def test_parenthesized_values_are_negative():
    data = {
        "financial_line_items": [
            {"label": "Net cash flows from operating activities", "values": {"2026": "(100)"}},
            {"label": "Net cash flows from investing activities", "values": {"2026": "50"}},
            {"label": "Net cash flow used in financing activities", "values": {"2026": "(50)"}},
            {"label": "Net increase in cash and cash equivalents", "values": {"2026": "(100)"}},
            {"label": "Cash and cash equivalents at the beginning of the year", "values": {"2026": 1000}},
            {"label": "Cash and cash equivalents at the end of the year", "values": {"2026": 900}},
        ],
        "periods": ["2026"],
    }
    result = validate(DocumentType.cash_flow_statement, data)
    assert result.checks[0].status == "PASS"
    assert result.checks[1].status == "PASS"


def test_missing_inputs_are_not_applicable():
    result = validate(DocumentType.invoice, {"fields": {}, "line_items": []})
    assert result.overall_status == "NOT_APPLICABLE"
    assert result.checks == []


def test_invoice_total_includes_shipping():
    data = {
        "fields": {
            "subtotal": {"value": 135},
            "tax_amount": {"value": 12.48},
            "shipping": {"value": 10},
            "discount": {"value": None},
            "total_amount": {"value": 157.48},
        },
        "line_items": [],
    }
    result = validate(DocumentType.invoice, data)
    check = next(c for c in result.checks if c.name == "invoice_total_check")
    assert check.status == "PASS"
    assert check.calculated_value == 157.48


def test_invoice_tolerance_is_absolute_not_percentage():
    data = {
        "fields": {"subtotal": {"value": 804}, "tax_amount": {"value": 63.47}, "shipping": {"value": 50}, "discount": {"value": 0}, "total_amount": {"value": 916.47}},
        "line_items": [],
    }
    result = validate(DocumentType.invoice, data)
    check = next(c for c in result.checks if c.name == "invoice_total_check")
    assert check.status == "FAIL"
    assert check.variance == 1.0


def test_row_matching_prefers_exact_label_over_short_substring():
    """Regression test for a real-world OCR bug: a short row like "Capital"
    is a literal substring of the alias "totalcapitalandliabilities", and an
    empty/garbled label (e.g. a stray ". ." OCR artifact) is a substring of
    every alias. Both used to be matched ahead of the real "Total" row because
    the old matcher accepted `label in alias` as well as `alias in label` and
    never excluded empty labels. Exact matches must always win.
    """
    data = {
        "financial_line_items": [
            {"label": "Capital", "values": {"2026": 100, "2025": 90}},
            {"label": ". .", "values": {"2026": 999999, "2025": 999999}},
            {"label": "Total Assets", "values": {"2026": 1000, "2025": 900}},
            {"label": "Total Capital and Liabilities", "values": {"2026": 1000, "2025": 900}},
        ],
        "periods": ["2026", "2025"],
    }
    result = validate(DocumentType.balance_sheet, data)
    for check in result.checks:
        assert check.calculated_value == check.reported_value
        assert check.status == "PASS"


def test_row_matching_ignores_empty_normalized_labels():
    """A label that normalizes to the empty string (pure punctuation/OCR
    noise) must never satisfy any alias lookup."""
    data = {
        "financial_line_items": [
            {"label": ". .", "values": {"2026": 42}},
            {"label": "Net cash flows from financing activities", "values": {"2026": 573776603}},
            {"label": "Net cash flows from operating activities", "values": {"2026": 172815931}},
            {"label": "Net cash used in investing activities", "values": {"2026": -11476802}},
            {"label": "Net increase in cash and cash equivalents", "values": {"2026": 735115732}},
        ],
        "periods": ["2026"],
    }
    result = validate(DocumentType.cash_flow_statement, data)
    check = next(c for c in result.checks if c.name == "cash_flow_net_change")
    assert check.operands["financing_cash_flow"] == 573776603
    assert check.operands["operating_cash_flow"] == 172815931
    assert check.operands["investing_cash_flow"] == -11476802
    assert check.status == "PASS"
