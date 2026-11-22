"""
Unit tests for `api.services.ResumeService`.

HOW TO MAINTAIN THIS FILE WHEN ADDING FEATURES:
-------------------------------------------------------------------------------
1. Added a new supported file format (e.g., `.txt` or `.rtf`)?
   -> Add magic bytes / parser tests under Section 2 (File Validation & Parsing).

2. Added new business rules to `process_resume_upload` (e.g., tier limits,
   custom credit costs, or extra ScanReport fields)?
   -> Update `mock_upload_deps` fixture and add a test under Section 3.

3. Modified credit deduction or refund rules?
   -> Update tests under Section 4 (Credit Deduction & Refund Idempotency).
-------------------------------------------------------------------------------
"""

from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from pypdf.errors import PdfStreamError
from rest_framework.exceptions import APIException, ValidationError

from api.models import ScanReport
from api.services import (
    DOCX_MAGIC_BYTES,
    MAX_RESUME_FILE_SIZE_BYTES,
    MAX_RESUME_TEXT_CHARS,
    PDF_MAGIC_BYTES,
    ResumeService,
)


# =============================================================================
# 1. FIXTURES (Reusable Mock Files, Users & DB/Transaction Patches)
# =============================================================================


@pytest.fixture
def valid_pdf_file():
    """Creates a minimal valid-signature PDF file in memory."""
    return SimpleUploadedFile(
        "resume.pdf",
        PDF_MAGIC_BYTES + b"1.4 sample content",
        content_type="application/pdf",
    )


@pytest.fixture
def valid_docx_file():
    """Creates a minimal valid-signature DOCX (ZIP header) file in memory."""
    return SimpleUploadedFile(
        "resume.docx",
        DOCX_MAGIC_BYTES + b"sample docx binary content",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@pytest.fixture
def mock_user(mocker):
    """
    Creates a lightweight User double whose class (`type(user)`) exposes a mock
    `.objects` manager—matching `type(user).objects.filter(...)` in services.py.
    """

    class DummyUser:
        objects = mocker.MagicMock()

        def __init__(self):
            self.pk = 101
            self.credits = 5
            self.refresh_from_db = mocker.MagicMock()

    return DummyUser()


@pytest.fixture
def mock_upload_deps(mocker, mock_user):
    """
    Patches Django DB transactions, ORM models, and Celery `.delay()` so
    `process_resume_upload` runs 100% in memory without a database or Redis.
    """
    # Bypass `transaction.set_rollback` & execute `on_commit` callbacks immediately
    mocker.patch("api.services.transaction.set_rollback")
    mocker.patch("api.services.transaction.on_commit", side_effect=lambda fn: fn())

    # Default: no existing resume found
    mocker.patch(
        "api.services.Resume.objects.filter",
        return_value=mocker.MagicMock(first=mocker.MagicMock(return_value=None)),
    )

    mock_resume = mocker.MagicMock(id=10, user=mock_user, file_name="resume.pdf")
    mock_report = mocker.MagicMock(id=99, status=ScanReport.Status.PENDING)

    deduct_mock = mocker.patch.object(ResumeService, "_deduct_credits")
    get_or_create_mock = mocker.patch.object(
        ResumeService, "_get_or_create_resume", return_value=mock_resume
    )
    create_report_mock = mocker.patch.object(
        ResumeService, "_create_scan_report", return_value=mock_report
    )
    create_tx_mock = mocker.patch.object(ResumeService, "_create_credit_transaction")
    delay_mock = mocker.patch("api.tasks.run_ai_review_task.delay")

    return SimpleNamespace(
        user=mock_user,
        resume=mock_resume,
        report=mock_report,
        deduct_credits=deduct_mock,
        get_or_create_resume=get_or_create_mock,
        create_scan_report=create_report_mock,
        create_credit_transaction=create_tx_mock,
        task_delay=delay_mock,
    )


# =============================================================================
# 2. FILE VALIDATION, HASHING & TEXT EXTRACTION TESTS
# =============================================================================


@pytest.mark.edge_case
def test_validate_uploaded_file_rejects_missing_or_oversized():
    """
    GIVEN None or a file exceeding MAX_RESUME_FILE_SIZE_BYTES (5 MB)
    WHEN  validate_uploaded_file is called
    THEN  it raises a DRF ValidationError.
    """
    with pytest.raises(ValidationError, match="No resume file provided"):
        ResumeService.validate_uploaded_file(None)

    oversized = SimpleNamespace(
        size=MAX_RESUME_FILE_SIZE_BYTES + 1,
        name="big.pdf",
        read=lambda n: PDF_MAGIC_BYTES,
        seek=lambda pos: None,
    )
    with pytest.raises(ValidationError, match="exceeds the 5 MB"):
        ResumeService.validate_uploaded_file(oversized)


@pytest.mark.edge_case
def test_validate_uploaded_file_checks_magic_bytes_and_extension():
    """
    GIVEN a file with a spoofed extension or unsupported format (.txt)
    WHEN  validate_uploaded_file is called
    THEN  it rejects mismatched magic signatures and resets file pointer to 0.
    """
    fake_pdf = SimpleUploadedFile("fake.pdf", b"NOT_A_PDF_HEADER")
    with pytest.raises(ValidationError, match="Invalid PDF file signature"):
        ResumeService.validate_uploaded_file(fake_pdf)
    assert fake_pdf.tell() == 0  # Pointer must be rewound

    fake_docx = SimpleUploadedFile("fake.docx", b"NOT_A_ZIP_HEADER")
    with pytest.raises(ValidationError, match="Invalid DOCX file signature"):
        ResumeService.validate_uploaded_file(fake_docx)

    unsupported = SimpleUploadedFile("notes.txt", b"Plain text resume")
    with pytest.raises(ValidationError, match="Unsupported file format"):
        ResumeService.validate_uploaded_file(unsupported)


@pytest.mark.happy_path
def test_compute_file_hash_returns_sha256_and_rewinds_stream(valid_pdf_file):
    """
    GIVEN a valid uploaded file
    WHEN  compute_file_hash is called
    THEN  it returns a 64-char SHA-256 hex digest and resets stream position to 0.
    """
    digest = ResumeService.compute_file_hash(valid_pdf_file)

    assert len(digest) == 64
    assert valid_pdf_file.tell() == 0


@pytest.mark.happy_path
def test_extract_resume_text_pdf_truncates_to_max_chars(mocker, valid_pdf_file):
    """
    GIVEN a PDF whose extracted text exceeds MAX_RESUME_TEXT_CHARS
    WHEN  extract_resume_text is called
    THEN  it joins page text and truncates safely to MAX_RESUME_TEXT_CHARS.
    """
    long_page = SimpleNamespace(extract_text=lambda: "A" * (MAX_RESUME_TEXT_CHARS + 500))
    mocker.patch("api.services.pypdf.PdfReader", return_value=SimpleNamespace(pages=[long_page]))

    result = ResumeService.extract_resume_text(valid_pdf_file)

    assert len(result) == MAX_RESUME_TEXT_CHARS


@pytest.mark.happy_path
def test_extract_resume_text_docx_success(mocker, valid_docx_file):
    """
    GIVEN a valid DOCX file with multiple paragraphs
    WHEN  extract_resume_text is called
    THEN  it extracts and joins non-empty paragraph text.
    """
    paragraphs = [
        SimpleNamespace(text="Jane Doe"),
        SimpleNamespace(text=""),
        SimpleNamespace(text="Senior Backend Developer"),
    ]
    mocker.patch(
        "api.services.docx.Document",
        return_value=SimpleNamespace(paragraphs=paragraphs),
    )

    result = ResumeService.extract_resume_text(valid_docx_file)

    assert result == "Jane Doe\nSenior Backend Developer"


@pytest.mark.edge_case
def test_extract_resume_text_handles_corrupt_or_blank_documents(mocker, valid_pdf_file):
    """
    GIVEN a corrupted PDF stream or a PDF with zero extractable text
    WHEN  extract_resume_text is called
    THEN  it raises a clean DRF ValidationError.
    """
    # 1. Corrupt PDF stream
    mocker.patch("api.services.pypdf.PdfReader", side_effect=PdfStreamError("broken xref"))
    with pytest.raises(ValidationError, match="Failed to parse document text"):
        ResumeService.extract_resume_text(valid_pdf_file)

    # 2. Scanned/image-only PDF (empty text)
    empty_page = SimpleNamespace(extract_text=lambda: "   ")
    mocker.patch("api.services.pypdf.PdfReader", return_value=SimpleNamespace(pages=[empty_page]))
    with pytest.raises(ValidationError, match="appears to be empty"):
        ResumeService.extract_resume_text(valid_pdf_file)


# =============================================================================
# 3. RESUME UPLOAD ORCHESTRATION TESTS
# =============================================================================


@pytest.mark.happy_path
def test_process_resume_upload_creates_scan_and_enqueues_task(
    mock_upload_deps, valid_pdf_file
):
    """
    GIVEN a new resume upload with valid text and job details
    WHEN  process_resume_upload runs (via `__wrapped__` to bypass @transaction.atomic)
    THEN  it deducts 1 credit, saves the resume/report, logs the transaction,
          and enqueues `run_ai_review_task.delay("99")` on commit.
    """
    resume, report = ResumeService.process_resume_upload.__wrapped__(
        user=mock_upload_deps.user,
        uploaded_file=valid_pdf_file,
        file_hash="abc123hash",
        extracted_text="Python Developer",
        job_title="  Backend Engineer  ",
        job_desc="  Build APIs  ",
    )

    assert resume == mock_upload_deps.resume
    assert report == mock_upload_deps.report
    mock_upload_deps.deduct_credits.assert_called_once_with(mock_upload_deps.user)
    mock_upload_deps.create_scan_report.assert_called_once_with(
        mock_upload_deps.resume, "Backend Engineer", "Build APIs"
    )
    mock_upload_deps.create_credit_transaction.assert_called_once_with(
        mock_upload_deps.user, valid_pdf_file, "Backend Engineer", 99
    )
    mock_upload_deps.task_delay.assert_called_once_with("99")


@pytest.mark.edge_case
def test_process_resume_upload_returns_existing_inflight_scan_without_charging(
    mocker, mock_upload_deps, valid_pdf_file
):
    """
    GIVEN the same user re-uploads an identical resume + job while a scan is PENDING/PROCESSING
    WHEN  process_resume_upload runs
    THEN  it returns the in-flight ScanReport immediately without deducting credits.
    """
    existing_resume = mocker.MagicMock(id=10)
    inflight_report = mocker.MagicMock(id=77, status=ScanReport.Status.PROCESSING)

    mocker.patch(
        "api.services.Resume.objects.filter",
        return_value=mocker.MagicMock(first=mocker.MagicMock(return_value=existing_resume)),
    )
    mocker.patch.object(
        ResumeService, "_get_inflight_scan", return_value=inflight_report
    )

    resume, report = ResumeService.process_resume_upload.__wrapped__(
        user=mock_upload_deps.user,
        uploaded_file=valid_pdf_file,
        file_hash="abc123hash",
        extracted_text="Python Developer",
        job_title="Backend Engineer",
        job_desc="Build APIs",
    )

    assert resume == existing_resume
    assert report == inflight_report
    mock_upload_deps.deduct_credits.assert_not_called()
    mock_upload_deps.task_delay.assert_not_called()


@pytest.mark.edge_case
def test_process_resume_upload_handles_race_condition_integrity_error(
    mocker, mock_upload_deps, valid_pdf_file
):
    """
    GIVEN a concurrent duplicate request triggers an IntegrityError on ScanReport.create
    WHEN  process_resume_upload catches IntegrityError
    THEN  it rolls back the transaction and returns the winning in-flight scan.
    """
    winning_report = mocker.MagicMock(id=88, status=ScanReport.Status.PENDING)
    mock_upload_deps.create_scan_report.side_effect = IntegrityError("duplicate inflight scan")
    mocker.patch.object(
        ResumeService, "_get_inflight_scan", return_value=winning_report
    )

    resume, report = ResumeService.process_resume_upload.__wrapped__(
        user=mock_upload_deps.user,
        uploaded_file=valid_pdf_file,
        file_hash="abc123hash",
        extracted_text="Python Developer",
        job_title="Backend Engineer",
        job_desc="Build APIs",
    )

    assert resume == mock_upload_deps.resume
    assert report == winning_report
    mock_upload_deps.task_delay.assert_not_called()


# =============================================================================
# 4. CREDIT DEDUCTION & REFUND IDEMPOTENCY TESTS
# =============================================================================


@pytest.mark.edge_case
def test_deduct_credits_raises_402_when_insufficient(mocker, mock_user):
    """
    GIVEN a user with 0 credits (atomic `.update()` affects 0 rows)
    WHEN  _deduct_credits is called
    THEN  it raises an APIException with code 402 and skips refresh_from_db.
    """
    mock_qs = mocker.MagicMock(update=mocker.MagicMock(return_value=0))
    type(mock_user).objects.filter.return_value = mock_qs

    with pytest.raises(APIException, match="Insufficient credits") as exc_info:
        ResumeService._deduct_credits(mock_user)

    assert exc_info.value.get_codes() == 402
    mock_user.refresh_from_db.assert_not_called()


@pytest.mark.happy_path
def test_refund_failed_scan_credit_refunds_once_and_blocks_duplicates(mocker, mock_user):
    """
    GIVEN a failed ScanReport that has NOT been refunded yet
    WHEN  refund_failed_scan_credit is called
    THEN  it increments user credits by 1, logs a CreditTransaction, and returns True.
          A second call for the same scan returns False without double-refunding.
    """
    mocker.patch("api.services.transaction.atomic")

    mock_resume = SimpleNamespace(user=mock_user, file_name="resume.pdf")
    mock_report = SimpleNamespace(id=55, resume=mock_resume)

    mocker.patch(
        "api.services.ScanReport.objects.select_related",
        return_value=mocker.MagicMock(
            filter=mocker.MagicMock(
                return_value=mocker.MagicMock(first=mocker.MagicMock(return_value=mock_report))
            )
        ),
    )

    # First call: already_refunded -> False; Second call: already_refunded -> True
    exists_mock = mocker.MagicMock(side_effect=[False, True])
    mocker.patch(
        "api.services.CreditTransaction.objects.filter",
        return_value=mocker.MagicMock(exists=exists_mock),
    )
    user_update_qs = mocker.MagicMock(update=mocker.MagicMock(return_value=1))
    type(mock_user).objects.filter.return_value = user_update_qs
    tx_create_mock = mocker.patch("api.services.CreditTransaction.objects.create")

    # 1st Refund Attempt -> Succeeds
    assert ResumeService.refund_failed_scan_credit(55) is True
    user_update_qs.update.assert_called_once()
    tx_create_mock.assert_called_once()

    # 2nd Refund Attempt (Idempotency guard) -> Blocked
    assert ResumeService.refund_failed_scan_credit(55) is False
    assert tx_create_mock.call_count == 1  # Still 1; no duplicate refund created