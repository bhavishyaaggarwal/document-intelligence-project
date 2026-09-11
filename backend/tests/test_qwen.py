import base64
import os
from pathlib import Path

import httpx
import pytest


@pytest.mark.asyncio
async def test_qwen_vision_integration():
    image_path = os.getenv("TEST_QWEN_IMAGE")
    if not image_path:
        pytest.skip("Set TEST_QWEN_IMAGE to an invoice image to run the local Qwen integration test.")

    image = Path(image_path)
    if not image.exists():
        pytest.fail(f"TEST_QWEN_IMAGE does not exist: {image}")

    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "qwen2.5vl:3b"),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "messages": [{
            "role": "user",
            "content": "Extract every visible invoice line item and return JSON with line_items, subtotal, tax_amount, shipping, and total_amount. Do not invent values.",
            "images": [encoded],
        }],
    }
    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/") + "/api/chat",
            json=payload,
        )
    response.raise_for_status()
    body = response.json()
    assert body.get("message", {}).get("content")
