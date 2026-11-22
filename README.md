# Production-Grade AI Resume Review System

An asynchronous, production-ready backend service for automated resume analysis powered by Django, Celery, PostgreSQL, and OpenRouter / OpenAI. Built with strict atomicity, tenant isolation, input validation, and asynchronous task resilience.

---

## Key Features & Architecture

```
[ Client Request ] 
       │
       ▼
[ Django View / Service Layer ] ──(Magic Bytes & File Check)──► [ Strict Validation ]
       │                                                                │
       ├──► Deduct Credit (Atomic F() Expressions)                      │
       ├──► Save Resume & ScanReport (PENDING)                         │
       │                                                                ▼
       └──► transaction.on_commit() ──► [ Celery Broker (Redis/RabbitMQ) ]
                                                        │
                                                        ▼
                                             [ Celery Worker Task ]
                                                        │
                                  ┌─────────────────────┴─────────────────────┐
                                  ▼                                           ▼
                     [ Return Early if DONE ]                     [ Check Soft/Hard Timeouts ]
                                  │                                           │
                                  └─────────────────────┬─────────────────────┘
                                                        │
                                                        ▼
                                          [ Call OpenRouter / LLM ]
                                                        │
                                       ┌────────────────┴────────────────┐
                                       ▼                                 ▼
                               [ Success ]                        [ Failure/Retry ]
                                       │                                 │
                            Validate Pydantic Schema           Retry w/ Exponential Backoff
                                       │                                 │
                                       ▼                                 ▼
                              Update Report (DONE)              Exhausted Retries (FAILED)
                                                                         │
                                                                         ▼
                                                            Refund Credit (Atomic)
```

---

## System Hardening & Core Architecture

### 1. Database & Query Isolation (`api/models.py`, `api/views.py`)
* **Partial Unique Constraints:** Prevents duplicate active scans (`PENDING` / `PROCESSING`) for identical file hashes and target roles per user.
* **Tenant Isolation:** All endpoints enforce tenant ownership checks (`resume__user=request.user`), returning `404 Not Found` on cross-user resource access to avoid leaking entity existence.
* **Optimized Indexing:** Composite indexes on hot lookup columns (e.g., `user_id`, `status`, `created_at`) ensure fast queries under heavy load.
* **Deterministic Sorting:** Paginated endpoints force deterministic ordering to prevent duplicate or missing records across pages.

### 2. Atomic Credits & Transactional Integrity (`api/services.py`)
* **Atomic Deductions:** Deducts credits atomically using Django `F("credits") - 1` expressions wrapped in `@transaction.atomic`.
* **Safe Task Dispatch (`transaction.on_commit`):** Celery tasks are dispatched *only after* database transactions commit, eliminating race conditions where worker tasks execute before `ScanReport` records are durable in PostgreSQL.
* **Idempotent Refunds:** Automatically refunds credits on permanent task failures using `F("credits") + 1` guarded by `CreditTransaction` audit logs to guarantee a 1-to-1 refund ratio.

### 3. File Security & Input Validation (`api/services.py`)
* **Magic Byte Checking:** Enforces strict binary signature verification (`%PDF-` for PDF, `PK\x03\x04` for DOCX) rather than relying on user-provided MIME types or extension strings.
* **Strict Size Bounds:** Hard 5 MB file upload limit enforced before file processing or database writes occur.
* **Text & Length Caps:** Strict input length checks on extracted resume text, job titles, and job descriptions to prevent token exhaust vulnerabilities and prompt-injection attacks.

### 4. Resilient Asynchronous Pipeline (`api/tasks.py`)
* **Idempotency Safeguards:** Immediately halts processing and returns early if a `ScanReport` has already achieved `DONE` status.
* **Timeout Bounds & Backoff:** Tasks define explicit `soft_time_limit` and `time_limit` parameters, handling `SoftTimeLimitExceeded` gracefully. Upstream LLM calls use jittered exponential backoff (`retry_backoff=True`, `retry_jitter=True`).
* **Structured Logging & Observability:** Replaces stdout print statements with structured logging (`logging.getLogger(__name__)`) annotated with `scan_report_id` for Docker log aggregation.
* **Schema Validation & Fallbacks:** Enforces structured Pydantic models (`ResumeAnalysisResponse`) and checks model `finish_reason == "length"` to guard against JSON truncation errors.

---

## 🛠️ API Workflow

### 1. Submit Resume for Review
`POST /api/v1/scans/upload/`
* **Payload:** `file` (PDF/DOCX), `target_role` (string), `job_description` (optional)
* **Response:**
```json
{
  "scan_report_id": "8f3a2c10-9b4e-4f12-a309-8bc34d8ef012",
  "status": "PENDING",
  "message": "Scan queued successfully."
}
```

### 2. Fetch Scan Status & Results
`GET /api/v1/scans/8f3a2c10-9b4e-4f12-a309-8bc34d8ef012/`
* **Response (In Progress):**
```json
{
  "id": "8f3a2c10-9b4e-4f12-a309-8bc34d8ef012",
  "status": "PROCESSING",
  "result": null
}
```
* **Response (Completed):**
```json
{
  "id": "8f3a2c10-9b4e-4f12-a309-8bc34d8ef012",
  "status": "DONE",
  "result": {
    "overall_score": 85,
    "category_scores": {
      "impact": 80,
      "brevity": 90
    },
    "key_strengths": ["Clear metrics in experience section"],
    "critical_improvements": ["Quantify achievements in recent role"]
  }
}
```

---

## 🧪 Verification & Test Coverage

The end-to-end pipeline has been verified against the following test scenarios:

| Category | Tested Condition | Expected Result | Status |
| :--- | :--- | :--- | :--- |
| **Throttling** | Rapid sequential requests per user | Returns `429 Too Many Requests` without leaking quota across tenants | Pass |
| **Validation** | Malformed headers / fake file extensions | Blocked by magic-byte signature check; 0 credits deducted | Pass |
| **Atomicity** | Simulated task failures & exhausted retries | Status updated to `FAILED`, 1 credit refunded atomically once | Pass |
| **Isolation** | User B queries User A's `scan_report_id` | Returns `404 Not Found` | Pass |
| **Idempotency**| Re-triggering Celery task on `DONE` scan | Task exits cleanly without calling LLM or wasting tokens | Pass |

---

## 🚀 Environment Setup

### Environment Variables
Define the following variables in your `.env` file:
```env
DEBUG=False
SECRET_KEY=your-django-secret-key
DATABASE_URL=postgres://user:password@db:5432/resumedb
CELERY_BROKER_URL=redis://redis:6379/0
OPENROUTER_API_KEY=your-openrouter-api-key
OPENAI_TIMEOUT=60.0
```

### Running with Docker Compose
```bash
# Build and start services
docker-compose up --build -d

# Run migrations
docker-compose exec web python manage.py migrate

# Run test suite
docker-compose exec web pytest
```