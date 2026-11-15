from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist

from .models import ScanReport


@shared_task
def run_ai_review_task(scan_report_id: int) -> None:
    scan_report = ScanReport.objects.get(id=scan_report_id)

    try:
        resume_text = scan_report.resume.parsed_text

        # TODO: AI processing

        scan_report.overall_score = 88
        scan_report.category_scores = {
            "formatting": 90,
            "experience": 85,
        }
        scan_report.missing_keywords = ["Docker", "FastAPI"]
        scan_report.status = ScanReport.Status.DONE

        scan_report.save(
            update_fields=[
                "overall_score",
                "category_scores",
                "missing_keywords",
                "status",
            ]
        )

    except Exception:  # noqa: BLE001
        ScanReport.objects.filter(id=scan_report_id).update(
            status=ScanReport.Status.FAILED
        )

        raise
