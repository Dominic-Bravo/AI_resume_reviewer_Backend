# api/services.py
# api/services.py
import hashlib

import docx
import pypdf
from django.db import transaction
from django.db.models import F
from rest_framework.exceptions import APIException, ValidationError

from .models import CreditTransaction, Resume, ScanReport


class ResumeService:
    @staticmethod
    def compute_file_hash(uploaded_file) -> str:
        file_bytes = uploaded_file.read()
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        uploaded_file.seek(0)
        return file_hash

    @staticmethod
    def extract_resume_text(uploaded_file) -> str:
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

        except Exception as e:
            raise ValidationError(f"Failed to parse document text: {str(e)}")

        extracted_text = extracted_text.strip()
        if not extracted_text:
            raise ValidationError("The uploaded document appears to be empty.")

        return extracted_text

    @staticmethod
    @transaction.atomic
    def process_resume_upload(
        user, uploaded_file, file_hash, extracted_text, job_title, job_desc
    ):
        """
        Handles both new resumes and re-scans of existing resume files against new/updated job targets.
        Atomically deducts credits, creates a fresh ScanReport, and enqueues the AI review after commit.
        """
        normalized_title = (job_title or "").strip()
        normalized_desc = (job_desc or "").strip()

        existing_resume = Resume.objects.filter(user=user, file_hash=file_hash).first()
        if existing_resume:
            inflight_scan = (
                ScanReport.objects.filter(
                    resume=existing_resume,
                    target_job_title=normalized_title,
                    target_job_desc=normalized_desc,
                    status__in=[
                        ScanReport.Status.PENDING,
                        ScanReport.Status.PROCESSING,
                    ],
                )
                .order_by("-id")
                .first()
            )
            if inflight_scan:
                return existing_resume, inflight_scan

        # Atomic conditional deduction prevents two concurrent requests from overdrawing credits
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

        # Get or create the resume record based on file hash and user
        resume, created = Resume.objects.get_or_create(
            user=user,
            file_hash=file_hash,
            defaults={
                "file_name": uploaded_file.name,
                "parsed_text": extracted_text,
            },
        )

        # If the resume already existed, keep filename and parsed_text current
        if not created:
            resume.file_name = uploaded_file.name
            resume.parsed_text = extracted_text
            resume.save(update_fields=["file_name", "parsed_text"])

        # ALWAYS create a brand new ScanReport so re-scans trigger fresh AI runs
        scan_report = ScanReport.objects.create(
            resume=resume,
            target_job_title=job_title,
            target_job_desc=job_desc,
            status=ScanReport.Status.PENDING,
        )

        CreditTransaction.objects.create(
            user=user,
            credits_added=-1,
            amount_cents=0,
            description=(
                f"Resume scan deduction for {uploaded_file.name} "
                f"({job_title or 'General Role'}) [scan={scan_report.id}]"
            ),
        )

        scan_id = scan_report.id

        def _enqueue_scan():
            from .tasks import run_ai_review_task

            run_ai_review_task.delay(scan_id)

        transaction.on_commit(_enqueue_scan)

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
            if not scan_report or not scan_report.resume_id:
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
