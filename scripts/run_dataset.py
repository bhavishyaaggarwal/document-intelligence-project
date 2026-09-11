"""Run the local extraction/validation pipeline over the supplied dataset.

Usage:
    python scripts/run_dataset.py "/path/to/New Dataset"
"""
import asyncio
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.schemas.document import DocumentType
from app.services.extraction_service import extract
from app.services.financial_validation_service import validate
from app.services.ocr_service import extract_text

TYPE_DIRS = {
    "Invoices": DocumentType.invoice,
    "Balance Sheet": DocumentType.balance_sheet,
    "Profit & Loss": DocumentType.profit_and_loss,
    "Cash Flows": DocumentType.cash_flow_statement,
}


async def main(root: Path):
    rows = []
    for directory, dtype in TYPE_DIRS.items():
        for path in sorted((root / directory).glob("*")):
            if path.suffix.lower() not in {".pdf", ".jpg", ".jpeg", ".png"}:
                continue
            ocr = extract_text(path.read_bytes(), path.name)
            extracted, method = await extract(dtype, ocr)
            validation = validate(dtype, extracted)
            rows.append({
                "file": str(path.relative_to(root)),
                "type": dtype.value,
                "pages": len(ocr.pages),
                "ocr_used": ocr.used_ocr,
                "extraction_method": method,
                "validation_status": validation.overall_status,
                "checks": len(validation.checks),
            })

    output = Path("docs/dataset_test_run.csv")
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Processed {len(rows)} files. Report: {output}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Pass the extracted dataset root, e.g. scripts/run_dataset.py '/data/New Dataset'")
    asyncio.run(main(Path(sys.argv[1])))
