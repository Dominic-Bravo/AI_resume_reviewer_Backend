import hashlib
import zipfile

import docx
import pypdf
from django.db import IntegrityError, transaction
from django.db.models import F
from pypdf.errors import PdfStreamError
from rest_framework.exceptions import APIException, ValidationError

from .models import CreditTransaction, Resume, ScanReport

MAX_RESUME_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_RESUME_TEXT_CHARS = 30_000
MAX_JOB_TITLE_CHARS = 255
MAX_JOB_DESC_CHARS = 10_000

PDF_MAGIC_BYTES = b"%PDF-"
DOCX_MAGIC_BYTES = b"PK\x03\x04"


class ResumeService:
    @staticmethod
    def validate_uploaded_file(uploaded_file) -> None:
        if uploaded_file is None:
            raise ValidationError("No resume file provided.")

        file_size = getattr(uploaded_file, "size", None)
        if file_size is not None and file_size > MAX_RESUME_FILE_SIZE_BYTES:
            raise ValidationError("Resume file exceeds the 5 MB maximum size limit.")

        filename = (getattr(uploaded_file, "name", "") or "").lower()
        header = uploaded_file.read(8)
        uploaded_file.seek(0)

        if not header:
            raise ValidationError("The uploaded file is empty.")

        if filename.endswith(".pdf"):
            if not header.startswith(PDF_MAGIC_BYTES):
                raise ValidationError("Invalid PDF file signature.")
        elif filename.endswith(".docx"):
            if not header.startswith(DOCX_MAGIC_BYTES):
                raise ValidationError("Invalid DOCX file signature.")
        else:
            raise ValidationError(
                "Unsupported file format. Please upload a PDF or DOCX file."
            )

    @staticmethod
    def compute_file_hash(uploaded_file) -> str:
        file_bytes = uploaded_file.read()
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        uploaded_file.seek(0)
        return file_hash

    @staticmethod
    def extract_resume_text(uploaded_file) -> str:
        ResumeService.validate_uploaded_file(uploaded_file)
        filename = uploaded_file.name.lower()
        extracted_text = ""

        try:
            if filename.endswith(".pdf"):
                reader = pypdf.PdfReader(uploaded_file)
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        extracted_text += text + "\n"
            elif filename.endswith(".docx"):
                doc = docx.Document(uploaded_file)
                for paragraph in doc.paragraphs:
                    if paragraph.text:
                        extracted_text += paragraph.text + "\n"
            else:
                raise ValidationError(
                    "Unsupported file format. Please upload a PDF or DOCX file."
                )

        except ValidationError:
            raise
        except (
            PdfStreamError,
            zipfile.BadZipFile,
            KeyError,
            ValueError,
            OSError,
        ) as exc:
            raise ValidationError(
                f"Failed to parse document text: {exc}"
            ) from exc

        extracted_text = extracted_text.strip()
        if not extracted_text:
            raise ValidationError("The uploaded document appears to be empty.")

        if len(extracted_text) > MAX_RESUME_TEXT_CHARS:
            extracted_text = extracted_text[:MAX_RESUME_TEXT_CHARS]

        return extracted_text

    @staticmethod
    def _get_inflight_scan(resume, job_title: str, job_desc: str):
        """Finds any pending or processing scan for a given resume and job target."""
        return (
            ScanReport.objects.filter(
                resume=resume,
                target_job_title=job_title,
                target_job_desc=job_desc,
                status__in=[ScanReport.Status.PENDING, ScanReport.Status.PROCESSING],
            )
            .order_by("-id")
            .first()
        )

    @staticmethod
    def _deduct_credits(user) -> None:
        """Atomically deducts 1 credit from user, raising APIException if insufficient."""
        updated_rows = (
            type(user)
            .objects.filter(pk=user.pk, credits__gte=1)
            .update(credits=F("credits") - 1)
        )
        if not updated_rows:
            raise APIException(
                "Insufficient credits. Please top up to analyze resumes.", code=402
            )
        user.refresh_from_db(fields=["credits"])

    @staticmethod
    def _get_or_create_resume(user, file_hash: str, uploaded_file, bounded_text: str) -> Resume:
        """Gets or creates a Resume record and ensures parsed text and file name are up-to-date."""
        resume, created = Resume.objects.get_or_create(
            user=user,
            file_hash=file_hash,
            defaults={
                "file_name": uploaded_file.name,
                "parsed_text": bounded_text,
            },
        )
        if not created:
            resume.file_name = uploaded_file.name
            resume.parsed_text = bounded_text
            resume.save(update_fields=["file_name", "parsed_text"])
        return resume

    @staticmethod
    def _create_scan_report(resume, job_title: str, job_desc: str) -> ScanReport:
        """Creates a new ScanReport for the resume and job target."""
        return ScanReport.objects.create(
            resume=resume,
            target_job_title=job_title,
            target_job_desc=job_desc,
            status=ScanReport.Status.PENDING,
        )

    @staticmethod
    def _create_credit_transaction(user, uploaded_file, job_title: str, scan_report_id) -> None:
        """Records the credit deduction transaction."""
        CreditTransaction.objects.create(
            user=user,
            credits_added=-1,
            amount_cents=0,
            description=(
                f"Resume scan deduction for {uploaded_file.name} "
                f"({job_title or 'General Role'}) [scan={scan_report_id}]"
            ),
        )

    @staticmethod
    def _enqueue_ai_task(scan_report_id) -> None:
        """Enqueues the Celery AI review task safely on transaction commit."""
        scan_id = str(scan_report_id)

        def _enqueue():
            from .tasks import run_ai_review_task

            run_ai_review_task.delay(scan_id)  # type: ignore[operator]

        transaction.on_commit(_enqueue)

    @staticmethod
    @transaction.atomic
    def process_resume_upload(
        user, uploaded_file, file_hash, extracted_text, job_title, job_desc
    ):
        """
        Handles both new resumes and re-scans of existing resume files against new/updated job targets.
        Atomically deducts credits, creates a fresh ScanReport, and enqueues the AI review after commit.
        """
        normalized_title = (job_title or "").strip()[:MAX_JOB_TITLE_CHARS]
        normalized_desc = (job_desc or "").strip()[:MAX_JOB_DESC_CHARS]
        bounded_text = (extracted_text or "").strip()[:MAX_RESUME_TEXT_CHARS]

        existing_resume = Resume.objects.filter(user=user, file_hash=file_hash).first()
        if existing_resume:
            inflight_scan = ResumeService._get_inflight_scan(
                existing_resume, normalized_title, normalized_desc
            )
            if inflight_scan:
                return existing_resume, inflight_scan

        ResumeService._deduct_credits(user)
        resume = ResumeService._get_or_create_resume(user, file_hash, uploaded_file, bounded_text)

        try:
            scan_report = ResumeService._create_scan_report(
                resume, normalized_title, normalized_desc
            )
        except IntegrityError:
            transaction.set_rollback(True)
            inflight_scan = ResumeService._get_inflight_scan(
                resume, normalized_title, normalized_desc
            )
            if inflight_scan:
                return resume, inflight_scan
            raise

        ResumeService._create_credit_transaction(
            user, uploaded_file, normalized_title, scan_report.id
        )
        ResumeService._enqueue_ai_task(scan_report.id)

        return resume, scan_report

    @staticmethod
    def refund_failed_scan_credit(scan_report_id) -> bool:
        """
        Atomically refunds 1 credit once when a scan permanently fails.
        Returns True if a refund was issued, False if already refunded or not applicable.
        """
        refund_marker = f"[scan={scan_report_id}] (refund)"

        with transaction.atomic():
            scan_report = (
                ScanReport.objects.select_related("resume__user")
                .filter(pk=scan_report_id)
                .first()
            )
            if not scan_report or scan_report.resume is None:
                return False

            user = scan_report.resume.user

            already_refunded = CreditTransaction.objects.filter(
                user=user,
                description__endswith=refund_marker,
            ).exists()
            if already_refunded:
                return False

            type(user).objects.filter(pk=user.pk).update(credits=F("credits") + 1)

            CreditTransaction.objects.create(
                user=user,
                credits_added=1,
                amount_cents=0,
                description=(
                    f"Credit refund for failed scan on {scan_report.resume.file_name} "
                    f"{refund_marker}"
                ),
            )
            return True