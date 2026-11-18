# api/services.py
# api/services.py
import hashlib

import docx
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
        Deducts credits and always creates a fresh ScanReport.
        """
        if user.credits < 1:
            raise APIException(
                "Insufficient credits. Please top up to analyze resumes.", code=402
            )

        # Deduct credit for the new scan
        user.credits -= 1
        user.save(update_fields=["credits"])

        CreditTransaction.objects.create(
            user=user,
            credits_added=-1,
            amount_cents=0,
            description=f"Resume scan deduction for {uploaded_file.name} ({job_title or 'General Role'})",
        )

        # Get or create the resume record based on file hash and user
        resume, created = Resume.objects.get_or_create(
            user=user,
            file_hash=file_hash,
            defaults={
                "file_name": uploaded_file.name,
                "parsed_text": extracted_text,
            },
        )

        # If the resume already existed but the file name changed or text was updated, update it
        if not created:
            resume.file_name = uploaded_file.name
            resume.parsed_text = extracted_text
            resume.save()

        # ALWAYS create a brand new ScanReport so re-scans trigger fresh AI runs
        scan_report = ScanReport.objects.create(
            resume=resume,
            target_job_title=job_title,
            target_job_desc=job_desc,
            status=ScanReport.Status.PENDING,
        )

        # Trigger async Celery task
        run_ai_review_task.delay(scan_report.id)

        return resume, scan_report
