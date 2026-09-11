# Deployment and runtime modes

This project supports local Qwen, hosted Gemini, and deterministic OCR fallback modes.

## 1. Full AI mode — local Qwen2.5-VL

For the complete zero-cost document-understanding workflow, run Ollama on the machine hosting the application:

```text
FastAPI -> Tesseract + page images -> Ollama/Qwen2.5-VL 3B -> Pydantic -> financial validation -> database -> dashboard
```

Required local configuration:

```env
EXTRACTION_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5vl:3b
OLLAMA_TIMEOUT_SECONDS=180
```

Confirm the model with `ollama list`. The application does not require OpenAI credits in this mode.

## 2. Hosted Gemini mode — public Render

The public deployment is configured for Gemini 3.5 Flash. Render does not need to host a vision model; the application sends rendered document pages plus OCR context to the Gemini API and validates the returned JSON locally.

Required Render environment variables:

```env
EXTRACTION_PROVIDER=gemini
GEMINI_API_KEY=<Render secret>
GEMINI_MODEL=gemini-3.5-flash
GEMINI_TIMEOUT_SECONDS=120
```

Never commit the API key. If Gemini is unavailable or the key is missing, the application falls back to deterministic OCR extraction rather than fabricating results.

## 3. Local Qwen mode

For local development with Ollama:

```env
EXTRACTION_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5vl:3b
OLLAMA_TIMEOUT_SECONDS=180
```

Do not expose an unauthenticated Ollama endpoint to the public internet.

## Render deployment

1. Create a **public** GitHub repository and push this project. Do not commit `.env`, database files, or API keys.
2. In Render choose **New -> Blueprint**, connect the GitHub repository, and use `render.yaml`.
3. Confirm the free web service and PostgreSQL database.
4. Wait for the service to become live and copy its generated `onrender.com` URL.

## Live acceptance checks

With the exact deployed URL (`LIVE_URL`), verify:

```text
GET  LIVE_URL/                         -> dashboard
GET  LIVE_URL/api/v1/health            -> 200
GET  LIVE_URL/docs                     -> Swagger
GET  LIVE_URL/api/v1/documents        -> 200
POST LIVE_URL/api/v1/documents/process -> supported document
GET  LIVE_URL/api/v1/documents/{name} -> latest result
```

The Render free PostgreSQL tier is appropriate for an assessment/demo but should not be represented as durable production storage.

## Submission URLs

Only record URLs that have actually been deployed and tested:

- Frontend: `LIVE_URL/`
- Backend: `LIVE_URL/`
- Swagger: `LIVE_URL/docs`
- GitHub: public repository URL
