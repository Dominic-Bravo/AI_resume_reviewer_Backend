# api/services.py
import hashlib

import pypdf
from django.db import transaction
from rest_framework.exceptions import APIException, ValidationError

from .models import CreditTransaction, Resume, ScanReport
from .tasks import run_ai_review_task


class ResumeService:
    @staticmethod
    def compute_file_hash(uploaded_file) -> str:
        file_bytes = uploaded_file.read()
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        uploaded_file.seek(0)
        return file_hash

    @staticmethod
    def extract_pdf_text(uploaded_file) -> str:
        extracted_text = ""
        try:
            reader = pypdf.PdfReader(uploaded_file)
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    extracted_text += text + "\n"
        except Exception as e:
            raise ValidationError(f"FAILED to parse PDF text: {str(e)}")

        extracted_text = extracted_text.strip()
        if not extracted_text:
            raise ValidationError(
                "The uploaded PDF appears to be empty or contains no selectable text."
            )
        return extracted_text

    @staticmethod
    def handle_idempotent_hit(existing_resume):
        latest_scan = existing_resume.scans.order_by("-created_at").first()

        if latest_scan and latest_scan.status == ScanReport.Status.FAILED:
            latest_scan.status = ScanReport.Status.PENDING
            latest_scan.save(update_fields=["status"])
            run_ai_review_task.delay(latest_scan.id)

            return {
                "message": "Previous scan failed. Retrying AI analysis...",
                "resume_id": existing_resume.id,
                "scan_id": latest_scan.id,
                "status": "PENDING",
            }, 200

        return {
            "message": "Idempotent hit: Resume already exists. No credits deducted.",
            "resume_id": existing_resume.id,
            "scan_id": latest_scan.id if latest_scan else None,
            "status": latest_scan.status if latest_scan else "UNKNOWN",
            "cached": True,
        }, 200

    @staticmethod
    @transaction.atomic
    def process_new_resume(
        user, uploaded_file, file_hash, extracted_text, job_title, job_desc
    ):
        if user.credits < 1:
            raise APIException(
                "Insufficient credits. Please top up to analyze new resumes.", code=402
            )

        # Deduct credit
        user.credits -= 1
        user.save(update_fields=["credits"])

        CreditTransaction.objects.create(
            user=user,
            credits_added=-1,
            amount_cents=0,
            description=f"Resume scan deduction for {uploaded_file.name}",
        )

        # Create records
        resume = Resume.objects.create(
            user=user,
            file_name=uploaded_file.name,
            file_hash=file_hash,
            parsed_text=extracted_text,
        )

        scan_report = ScanReport.objects.create(
            resume=resume,
            target_job_title=job_title,
            target_job_desc=job_desc,
            status=ScanReport.Status.PENDING,
        )

        # Trigger async Celery task
        run_ai_review_task.delay(scan_report.id)

        return resume, scan_report
