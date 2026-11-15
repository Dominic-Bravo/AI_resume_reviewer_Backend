import os

from celery import shared_task
from google import genai
from pydantic import BaseModel

from .models import ScanReport


class ResumeAnalysisResponse(BaseModel):
    overall_score: int
    category_scores: dict
    missing_keywords: list[str]
    bullet_critiques: list[str]


@shared_task
def run_ai_review_task(scan_report_id):
    try:
        scan_report = ScanReport.objects.get(id=scan_report_id)
        resume_text = scan_report.resume.parsed_text
        job_title = scan_report.target_job_title or "General Software Role"
        job_desc = scan_report.target_job_desc or "Standard professional expectations."

        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

        prompt = f"""
        You are an expert technical recruiter and resume reviewer. 
        Analyze the following resume text against the target job title and description.
        
        Target Job Title: {job_title}
        Job Description: {job_desc}
        
        Resume Text:
        {resume_text}
        """

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": ResumeAnalysisResponse,
            },
        )

        result_data = ResumeAnalysisResponse.model_validate_json(response.text)

        scan_report.overall_score = result_data.overall_score
        scan_report.category_scores = result_data.category_scores
        scan_report.missing_keywords = result_data.missing_keywords
        scan_report.bullet_critiques = result_data.bullet_critiques
        scan_report.status = ScanReport.Status.DONE
        scan_report.save()

    except Exception as e:
        try:
            scan_report = ScanReport.objects.get(id=scan_report_id)
            scan_report.status = ScanReport.Status.FAILED
            scan_report.save()
        except Exception:
            pass
        raise e
