import math
import re
from typing import Any
from app.core.config import get_settings
from app.schemas.document import DocumentType, ValidationCheck, ValidationResult


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        neg = s.startswith("(") and s.endswith(")")
        s = re.sub(r"[^\d.\-]", "", s)
        if not s:
            return None
        try:
            x = float(s)
            return -abs(x) if neg else x
        except ValueError:
            return None
    return None


def _check(name, formula, operands, calculated, reported, period=None):
    if calculated is None or reported is None:
        return ValidationCheck(
            name=name, formula=formula, operands=operands,
            calculated_value=calculated, reported_value=reported,
            variance=None, status="NOT_APPLICABLE", period=period
        )
    variance = calculated - reported
    # validation_tolerance is an absolute monetary tolerance (e.g. 0.01 = one cent),
    # not a percentage of the reported amount. This prevents a $1 discrepancy from
    # passing simply because the invoice total is large.
    tol = max(0.0, get_settings().validation_tolerance)
    return ValidationCheck(
        name=name, formula=formula, operands=operands,
        calculated_value=calculated, reported_value=reported,
        variance=variance, status="PASS" if abs(variance) <= tol else "FAIL", period=period
    )


def _field_values(extracted: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (v.get("value") if isinstance(v, dict) else v)
        for k, v in extracted.get("fields", {}).items()
    }


def _find_field(fields: dict[str, Any], *names: str) -> float | None:
    lower = {re.sub(r"[^a-z0-9]", "", k.lower()): _num(v) for k, v in fields.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in lower and lower[key] is not None:
            return lower[key]
    return None


def _invoice(extracted: dict[str, Any]) -> list[ValidationCheck]:
    fields = _field_values(extracted)
    checks = []
    items = extracted.get("line_items") or []

    item_calcs = []
    for idx, item in enumerate(items):
        q, p, amount = _num(item.get("quantity")), _num(item.get("unit_price")), _num(item.get("amount"))
        checks.append(_check(
            f"invoice_line_{idx+1}_total", "quantity × unit_price",
            {"quantity": q, "unit_price": p}, q * p if q is not None and p is not None else None,
            amount
        ))
        if amount is not None:
            item_calcs.append(amount)

    subtotal = _find_field(fields, "subtotal")
    tax = _find_field(fields, "tax_amount", "gst_amount", "tax")
    discount = _find_field(fields, "discount")
    shipping = _find_field(fields, "shipping", "shipping_amount", "freight", "handling")
    total = _find_field(fields, "total_amount", "total", "amount_due")

    if item_calcs and subtotal is not None:
        checks.append(_check(
            "invoice_items_subtotal_check", "sum(line_item.amount)",
            {"line_item_total": sum(item_calcs)}, sum(item_calcs), subtotal
        ))

    if subtotal is not None and total is not None and tax is not None:
        discount = discount or 0.0
        shipping = shipping or 0.0
        checks.append(_check(
            "invoice_total_check", "subtotal + tax_amount + shipping - discount",
            {"subtotal": subtotal, "tax_amount": tax, "shipping": shipping, "discount": discount},
            subtotal + tax + shipping - discount, total
        ))
    elif item_calcs and total is not None and tax is not None:
        checks.append(_check(
            "invoice_items_plus_tax_check", "sum(line_item.amount) + tax_amount",
            {"line_item_total": sum(item_calcs), "tax_amount": tax},
            sum(item_calcs) + tax, total
        ))

    cash = _find_field(fields, "cash_paid", "cash")
    change = _find_field(fields, "change")
    if cash is not None and total is not None and change is not None:
        checks.append(_check(
            "invoice_cash_change_check", "cash paid - total amount",
            {"cash_paid": cash, "total_amount": total},
            cash - total, change
        ))
    return checks


def _financial_rows(extracted: dict[str, Any]) -> dict[str, dict[str, dict[str, float | None]]]:
    rows = {}
    for row in extracted.get("financial_line_items", []):
        label = re.sub(r"[^a-z0-9]", "", row.get("label", "").lower())
        rows[label] = row.get("values", {})
    return rows


def _row_value(rows, aliases, period):
    """Return a value for the requested period using normalized semantic aliases.

    A single-period row is intentionally NOT reused for another comparative period.
    This preserves the case-study rule that missing/unreadable cells become null
    rather than being copied across columns.

    Matching is ranked by specificity (exact label match, then alias-contained-
    in-label) rather than "first row encountered". The previous implementation
    also treated `label in alias` as a match, which let short/garbled labels
    (including OCR noise that normalizes to an empty string, e.g. ". .") match
    almost any alias -- since a short or empty string is trivially a substring
    of a long alias. That caused unrelated rows (or completely blank rows) to
    be picked up ahead of the real one. Empty/very short normalized labels are
    now excluded outright, and only "alias appears inside the label" is
    accepted as a fuzzy match, never the reverse.
    """
    normalized_aliases = [re.sub(r"[^a-z0-9]", "", a.lower()) for a in aliases]
    exact_match: float | None = None
    fuzzy_match: float | None = None
    for label, values in rows.items():
        if not label or len(label) < 3 or period not in values:
            continue
        value = _num(values[period])
        if value is None:
            continue
        for alias in normalized_aliases:
            if alias == label:
                if exact_match is None:
                    exact_match = value
            elif alias in label:
                if fuzzy_match is None:
                    fuzzy_match = value
    return exact_match if exact_match is not None else fuzzy_match


def _periods(extracted):
    periods = extracted.get("periods") or []
    if periods:
        return periods
    vals = []
    for row in extracted.get("financial_line_items", []):
        vals.extend(row.get("values", {}).keys())
    return list(dict.fromkeys(vals))[:6]


def _balance_sheet(extracted):
    rows = _financial_rows(extracted)
    checks = []
    for period in _periods(extracted):
        assets = _row_value(rows, ["totalassets"], period)
        liabilities = _row_value(rows, ["totalcapitalandliabilities", "capitallabilities"], period)
        if liabilities is None:
            liabilities = _row_value(rows, ["totalliabilities"], period)
        checks.append(_check(
            "balance_sheet_balance",
            "total capital & liabilities ≈ total assets",
            {"total_capital_and_liabilities": liabilities, "total_assets": assets},
            liabilities, assets, period
        ))
    return checks


def _pnl(extracted):
    rows = _financial_rows(extracted)
    checks = []
    for period in _periods(extracted):
        interest = _row_value(rows, ["interestearned"], period)
        other = _row_value(rows, ["otherincome"], period)
        total_income = _row_value(rows, ["totalincome"], period)
        if interest is not None and other is not None and total_income is not None:
            checks.append(_check(
                "pnl_total_income", "interest earned + other income",
                {"interest_earned": interest, "other_income": other},
                interest + other, total_income, period
            ))

        ie = _row_value(rows, ["interestexpended"], period)
        op = _row_value(rows, ["operatingexpenses"], period)
        prov = _row_value(rows, ["provisionsandcontingencies"], period)
        total_exp = _row_value(rows, ["totalexpenditure"], period)
        if ie is not None and op is not None and prov is not None and total_exp is not None:
            checks.append(_check(
                "pnl_total_expenditure",
                "interest expended + operating expenses + provisions & contingencies",
                {"interest_expended": ie, "operating_expenses": op, "provisions_and_contingencies": prov},
                ie + op + prov, total_exp, period
            ))

        profit_before = _row_value(rows, ["consolidatednetprofitfortheyearbeforeminorityinterest"], period)
        net_profit = _row_value(rows, ["consolidatednetprofitfortheyearattributabletothegroup"], period)
        minority = _row_value(rows, ["minorityinterest"], period)
        if profit_before is not None and minority is not None and net_profit is not None:
            checks.append(_check(
                "pnl_group_net_profit",
                "profit before minority interest - minority interest",
                {"profit_before_minority_interest": profit_before, "minority_interest": minority},
                profit_before - minority, net_profit, period
            ))
    return checks


def _cash_flow(extracted):
    rows = _financial_rows(extracted)
    checks = []
    for period in _periods(extracted):
        op = _row_value(rows, [
            "netcashflowsfromoperatingactivities", "netcashfromoperatingactivities",
            "netcashflowfromoperatingactivities", "netcashflowusedinfromoperatingactivities",
            "netcashflowusedinoperatingactivities",
        ], period)
        inv = _row_value(rows, [
            "netcashflowsfrominvestingactivities", "netcashflowfromusedininvestingactivities",
            "netcashflowsfromusedininvestingactivities", "netcashusedininvestingactivities",
            "netcashflowsusedininvestingactivities", "cashflowusedininvestingactivities",
            "cashflowsusedininvestingactivities",
        ], period)
        fin = _row_value(rows, [
            "netcashflowsfromfinancingactivities", "netcashusedinfromfinancingactivities",
            "netcashusedinfinancingactivities", "netcashflowusedinfinancingactivities", "netcashfromfinancingactivities",
            "netcashgeneratedfromfinancingactivities",
        ], period)
        fx = _row_value(rows, ["effectoffluctuationinforeigncurrencytranslationreserve", "effectofexchangefluctuationontranslationreserve", "fxtranslationadjustment"], period) or 0.0
        amalg = _row_value(rows, ["cashandcashequivalentsonamalgamation", "cashandcashequivalentsonamalgamation"], period) or 0.0
        net = _row_value(rows, ["netincreaseincashandcashequivalents", "netincreasedecreaseincashandcashequivalents", "netincreaseincash"], period)
        checks.append(_check(
            "cash_flow_net_change",
            "operating + investing + financing + FX/translation adjustment + amalgamation cash",
            {"operating_cash_flow": op, "investing_cash_flow": inv, "financing_cash_flow": fin, "fx_adjustment": fx, "amalgamation_cash": amalg},
            op + inv + fin + fx + amalg if None not in (op, inv, fin) else None,
            net, period
        ))
        opening = _row_value(rows, ["cashandcashequivalentsatthebeginningoftheyear", "openingcash"], period)
        closing = _row_value(rows, ["cashandcashequivalentsattheendoftheyear", "closingcash"], period)
        if opening is not None and net is not None:
            checks.append(_check(
                "cash_flow_closing_cash",
                "opening cash + net increase in cash",
                {"opening_cash": opening, "net_increase_in_cash": net},
                opening + net, closing, period
            ))
    return checks


def validate(document_type: DocumentType, extracted: dict[str, Any]) -> ValidationResult:
    if document_type == DocumentType.invoice:
        checks = _invoice(extracted)
    elif document_type == DocumentType.balance_sheet:
        checks = _balance_sheet(extracted)
    elif document_type == DocumentType.profit_and_loss:
        checks = _pnl(extracted)
    else:
        checks = _cash_flow(extracted)

    statuses = [c.status for c in checks]
    if "FAIL" in statuses:
        overall = "FAIL"
    elif checks and all(s == "PASS" for s in statuses):
        overall = "PASS"
    else:
        overall = "NOT_APPLICABLE"
    issues = [f"{c.name} failed" for c in checks if c.status == "FAIL"]
    return ValidationResult(checks=checks, overall_status=overall, issues=issues)
