"""
Unit tests for `api.tasks.run_ai_review_task`.

HOW TO MAINTAIN THIS FILE WHEN ADDING FEATURES:
-------------------------------------------------------------------------------
1. Added a new field to `ResumeAnalysisResponse` (Pydantic schema in tasks.py)?
   -> Update `valid_openrouter_completion` fixture with the new field.
   -> Add an assertion in `test_run_ai_review_task_success`.

2. Added a new field to `ScanReport` or `Resume` models?
   -> Add the default attribute to `mock_scan_report` fixture.

3. Added a new external service/API call inside `run_ai_review_task`?
   -> Patch it inside `mock_task_dependencies` (or in your specific test) using
      the path where it is imported: `"api.tasks.<YourService>"`.

4. Added a new failure/edge-case branch?
   -> Copy an existing test, follow the Arrange / Act / Assert blocks, and tag
      it with an appropriate `@pytest.mark.<category>`.
-------------------------------------------------------------------------------
"""

import json
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry, SoftTimeLimitExceeded

from api.models import ScanReport
from api.tasks import run_ai_review_task

# =============================================================================
# 1. FIXTURES (Reusable Mock Objects & Dependencies)
# =============================================================================


@pytest.fixture
def mock_scan_report(mocker):
    """
    In-memory mock of `ScanReport` + related `Resume` (avoids database hits).
    Update this fixture whenever new fields are accessed on `scan_report`.
    """
    resume = SimpleNamespace(parsed_text="Experienced Python & Django Backend Engineer.")

    report = mocker.MagicMock(spec=ScanReport)
    report.id = 42
    report.status = ScanReport.Status.PENDING
    report.resume = resume
    report.target_job_title = "Senior Backend Engineer"
    report.target_job_desc = "Looking for Python, Django, Celery, and PostgreSQL experience."
    return report


@pytest.fixture
def valid_openrouter_completion():
    """
    Mock OpenRouter/OpenAI response adhering to `ResumeAnalysisResponse`.
    Update `payload` whenever the Pydantic output schema in `tasks.py` changes.
    """
    payload = {
        "overall_score": 85,
        "category_scores": [
            {"category": "technical_skills", "score": 90},
            {"category": "experience", "score": 80},
            {"category": "impact_metrics", "score": 85},
        ],
        "missing_keywords": ["Celery", "PostgreSQL"],
        "section_critiques": [
            {
                "section": "Experience",
                "target_item": "Backend Engineer",
                "critique": "Add performance metrics.",
                "suggested_rewrite": "Reduced API latency by 40% using Django and Redis.",
            }
        ],
    }
    message = SimpleNamespace(content=json.dumps(payload))
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=150, completion_tokens=120, total_tokens=270)
    return SimpleNamespace(choices=[choice], usage=usage)


@pytest.fixture
def mock_deps(mocker, mock_scan_report, valid_openrouter_completion):
    """
    Centralizes all external dependency patches (ORM, OpenAI client, Credit Service)
    so individual tests only override what they specifically want to test.
    """
    # Mock ScanReport.objects.select_related("resume").get(id=...)
    mocker.patch(
        "api.tasks.ScanReport.objects.select_related",
        return_value=mocker.MagicMock(get=mocker.MagicMock(return_value=mock_scan_report)),
    )
    # Mock ScanReport.objects.filter(id=...).first() (used in failure handler)
    mocker.patch(
        "api.tasks.ScanReport.objects.filter",
        return_value=mocker.MagicMock(first=mocker.MagicMock(return_value=mock_scan_report)),
    )

    # Mock OpenAI / OpenRouter client
    mock_openai_cls = mocker.patch("api.tasks.OpenAI")
    mock_openai_cls.return_value.chat.completions.create.return_value = (
        valid_openrouter_completion
    )

    # Mock credit refund service
    mock_refund = mocker.patch("api.tasks.ResumeService.refund_failed_scan_credit")

    return SimpleNamespace(
        report=mock_scan_report,
        openai_cls=mock_openai_cls,
        openai_create=mock_openai_cls.return_value.chat.completions.create,
        refund_credit=mock_refund,
    )


# =============================================================================
# 2. HAPPY PATH & IDEMPOTENCY TESTS
# =============================================================================


@pytest.mark.happy_path
def test_run_ai_review_task_success(mock_deps):
    """
    GIVEN a pending ScanReport with valid resume text and a valid LLM JSON response
    WHEN  run_ai_review_task executes
    THEN  scores, keywords, and critiques are saved and status transitions to DONE.
    """
    # Act: Execute synchronously via .run() (bypasses Celery/Redis)
    run_ai_review_task.run(scan_report_id=42)

    # Assert: Verify mapped output fields
    report = mock_deps.report
    assert report.overall_score == 85
    assert report.category_scores == {
        "technical_skills": 90,
        "experience": 80,
        "impact_metrics": 85,
    }
    assert report.missing_keywords == ["Celery", "PostgreSQL"]
    assert len(report.section_critiques) == 1

    # Assert: Verify lifecycle transitions (PENDING -> PROCESSING -> DONE)
    assert report.status == ScanReport.Status.DONE
    assert report.save.call_count == 2
    mock_deps.refund_credit.assert_not_called()


@pytest.mark.happy_path
def test_run_ai_review_task_skips_already_done(mock_deps):
    """
    GIVEN a ScanReport that already has status == DONE (e.g., duplicate Celery delivery)
    WHEN  run_ai_review_task executes
    THEN  it exits immediately without calling OpenRouter or saving again.
    """
    # Arrange
    mock_deps.report.status = ScanReport.Status.DONE

    # Act
    run_ai_review_task.run(scan_report_id=42)

    # Assert
    mock_deps.openai_cls.assert_not_called()
    mock_deps.report.save.assert_not_called()


# =============================================================================
# 3. VALIDATION & EDGE-CASE TESTS
# =============================================================================


@pytest.mark.edge_case
def test_run_ai_review_task_empty_resume_refunds_credit(mock_deps):
    """
    GIVEN a ScanReport whose parsed resume text is blank/whitespace
    WHEN  run_ai_review_task executes
    THEN  it skips the LLM call, marks status FAILED, and refunds the user's credit.
    """
    # Arrange
    mock_deps.report.resume.parsed_text = "   \n\t  "

    # Act
    run_ai_review_task.run(scan_report_id=42)

    # Assert
    mock_deps.openai_cls.assert_not_called()
    assert mock_deps.report.status == ScanReport.Status.FAILED
    mock_deps.refund_credit.assert_called_once_with(42)


# =============================================================================
# 4. RETRY & FAILURE RECOVERY TESTS
# =============================================================================


@pytest.mark.retries
def test_run_ai_review_task_invalid_json_triggers_retry(mocker, mock_deps):
    """
    GIVEN OpenRouter returns truncated/invalid JSON while retries remain
    WHEN  run_ai_review_task executes
    THEN  it triggers Celery's self.retry() and does NOT refund credits yet.
    """
    # Arrange: Return malformed JSON
    bad_choice = SimpleNamespace(
        message=SimpleNamespace(content="{invalid json"),
        finish_reason="length",
    )
    mock_deps.openai_create.return_value = SimpleNamespace(choices=[bad_choice], usage=None)
    mock_retry = mocker.patch.object(
        run_ai_review_task, "retry", side_effect=Retry("Retrying task")
    )

    # Act & Assert
    with pytest.raises(Retry):
        run_ai_review_task.run(scan_report_id=42)

    mock_retry.assert_called_once()
    mock_deps.refund_credit.assert_not_called()


@pytest.mark.retries
def test_run_ai_review_task_max_retries_exhausted_marks_failed_and_refunds(
    mocker, mock_deps
):
    """
    GIVEN an API timeout occurs on the final retry attempt (retries == max_retries)
    WHEN  run_ai_review_task executes
    THEN  it marks the report as FAILED, refunds the scan credit, and re-raises.
    """
    # Arrange: Simulate timeout at max_retries (3)
    mock_deps.openai_create.side_effect = SoftTimeLimitExceeded()
    mocker.patch.object(run_ai_review_task.request, "retries", 3)

    # Act & Assert
    with pytest.raises(SoftTimeLimitExceeded):
        run_ai_review_task.run(scan_report_id=42)

    assert mock_deps.report.status == ScanReport.Status.FAILED
    mock_deps.refund_credit.assert_called_once_with(42)