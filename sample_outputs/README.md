# Sample outputs

These JSON files were produced by running the application pipeline against files from the supplied dataset on 2026-09-11.

They use `ocr_fallback` because no external LLM API key was available in the build environment. They therefore demonstrate real ingestion/OCR/schema/validation behavior, not ground-truth accuracy.

For the final assessment demo, configure `OPENAI_API_KEY` and use a compatible vision-capable model so the extraction stage can use both OCR text and page images. Do not edit these samples to make them look better; regenerate them from actual processing if the extractor changes.
