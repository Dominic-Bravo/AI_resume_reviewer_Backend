"""
Unit tests for `api.serializers`.

HOW TO MAINTAIN THIS FILE WHEN ADDING FEATURES:
-------------------------------------------------------------------------------
1. Added a new validation rule to `RegisterSerializer` or `LoginSerializer`?
   -> Add a test under Section 2 (Authentication Serializers).

2. Added or renamed fields on `ScanReportSerializer` or `ResumeHistorySerializer`?
   -> Update the mock objects and expected keys under Section 3 (Resume & Scan
      Output Serializers) so frontend OpenAPI/Orval contracts stay locked in.
-------------------------------------------------------------------------------
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from api.models import ScanReport
from api.serializers import (
    LoginSerializer,
    RegisterSerializer,
    ResumeHistorySerializer,
    ScanReportSerializer,
    UploadResumeSerializer,
)

# =============================================================================
# 1. FIXTURES (Reusable Mock ORM Querysets & Report Objects)
# =============================================================================


@pytest.fixture
def mock_no_existing_user(mocker):
    """
    Mocks `User.objects.filter(...).exists()` to return False so DRF's
    UniqueValidator and `validate_email` pass without hitting a database.
    """
    qs = mocker.MagicMock(exists=mocker.MagicMock(return_value=False))
    # DRF UniqueValidator calls `.exclude(pk=...)` on updates, `.filter(...)` on creates
    qs.exclude.return_value = qs
    return mocker.patch("api.serializers.User.objects.filter", return_value=qs)


@pytest.fixture
def sample_scan_report():
    """Creates a realistic in-memory ScanReport object for serialization tests."""
    resume = SimpleNamespace(file_name="backend_resume.pdf")
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        resume=resume,
        target_job_title="Senior Python Engineer",
        target_job_desc="Django, Celery, PostgreSQL",
        overall_score=88,
        category_scores={"technical_skills": 92, "experience": 84},
        missing_keywords=["Kubernetes"],
        section_critiques=[
            {
                "section": "Experience",
                "target_item": "Backend Lead",
                "critique": "Quantify throughput gains.",
                "suggested_rewrite": "Scaled API to 5k req/s with Redis caching.",
            }
        ],
        status=ScanReport.Status.DONE,
        created_at=datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc),
    )


# =============================================================================
# 2. AUTHENTICATION SERIALIZER TESTS (Register & Login)
# =============================================================================


@pytest.mark.happy_path
def test_register_serializer_normalizes_email_and_creates_user(
    mocker, mock_no_existing_user
):
    """
    GIVEN valid registration data with uppercase/padded email and matching passwords
    WHEN  RegisterSerializer validates and creates the user
    THEN  email is lowercased/stripped, `password_confirm` is stripped, and `create_user` is called.
    """
    payload = {
        "email": "  Jane.Doe@Example.COM  ",
        "password": "StrongPassword123",
        "password_confirm": "StrongPassword123",
    }
    serializer = RegisterSerializer(data=payload)

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["email"] == "jane.doe@example.com"

    mock_created_user = SimpleNamespace(id=1, email="jane.doe@example.com")
    create_user_mock = mocker.patch(
        "api.serializers.User.objects.create_user", return_value=mock_created_user
    )

    # Call `__wrapped__` to bypass `@transaction.atomic` without a DB connection
    user = serializer.create.__wrapped__(serializer, dict(serializer.validated_data))

    assert user == mock_created_user
    create_user_mock.assert_called_once_with(
        email="jane.doe@example.com",
        password="StrongPassword123",
    )


@pytest.mark.edge_case
def test_register_serializer_rejects_duplicate_email_and_mismatched_passwords(mocker):
    """
    GIVEN an already-registered email OR mismatched password/password_confirm
    WHEN  RegisterSerializer.is_valid() runs
    THEN  validation fails with clear field-level error messages.
    """
    # 1. Duplicate email
    qs_exists = mocker.MagicMock(exists=mocker.MagicMock(return_value=True))
    mocker.patch("api.serializers.User.objects.filter", return_value=qs_exists)

    dup_serializer = RegisterSerializer(
        data={
            "email": "taken@example.com",
            "password": "StrongPassword123",
            "password_confirm": "StrongPassword123",
        }
    )
    assert not dup_serializer.is_valid()
    assert "email" in dup_serializer.errors

    # 2. Mismatched passwords
    qs_free = mocker.MagicMock(exists=mocker.MagicMock(return_value=False))
    mocker.patch("api.serializers.User.objects.filter", return_value=qs_free)

    mismatch_serializer = RegisterSerializer(
        data={
            "email": "new@example.com",
            "password": "StrongPassword123",
            "password_confirm": "DifferentPassword456",
        }
    )
    assert not mismatch_serializer.is_valid()
    assert "password_confirm" in mismatch_serializer.errors
    assert "Passwords do not match." in str(mismatch_serializer.errors["password_confirm"])


@pytest.mark.happy_path
def test_login_serializer_authenticates_active_user(mocker):
    """
    GIVEN valid credentials for an active user
    WHEN  LoginSerializer validates
    THEN  `authenticate()` receives normalized email and attaches `user` to `validated_data`.
    """
    active_user = SimpleNamespace(id=7, email="dev@example.com", is_active=True)
    auth_mock = mocker.patch("api.serializers.authenticate", return_value=active_user)

    serializer = LoginSerializer(
        data={"email": "  DEV@Example.com ", "password": "secret123"}
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["user"] == active_user
    auth_mock.assert_called_once_with(
        request=None,
        username="dev@example.com",
        password="secret123",
    )


@pytest.mark.edge_case
def test_login_serializer_rejects_invalid_credentials_or_disabled_account(mocker):
    """
    GIVEN bad credentials (`authenticate` returns None) OR an inactive user (`is_active=False`)
    WHEN  LoginSerializer.is_valid() runs
    THEN  validation fails with the appropriate error message.
    """
    # 1. Invalid credentials
    mocker.patch("api.serializers.authenticate", return_value=None)
    bad_login = LoginSerializer(data={"email": "x@example.com", "password": "wrong"})
    assert not bad_login.is_valid()
    assert "Invalid email or password." in str(bad_login.errors)

    # 2. Inactive user
    inactive_user = SimpleNamespace(id=8, email="disabled@example.com", is_active=False)
    mocker.patch("api.serializers.authenticate", return_value=inactive_user)
    disabled_login = LoginSerializer(
        data={"email": "disabled@example.com", "password": "secret"}
    )
    assert not disabled_login.is_valid()
    assert "User account is disabled." in str(disabled_login.errors)


# =============================================================================
# 3. RESUME UPLOAD & REPORT OUTPUT SERIALIZER TESTS
# =============================================================================


@pytest.mark.happy_path
def test_upload_resume_serializer_allows_optional_job_fields():
    """
    GIVEN only a resume file (without target_job_title or target_job_desc)
    WHEN  UploadResumeSerializer validates
    THEN  it passes validation cleanly.
    """
    pdf = SimpleUploadedFile("cv.pdf", b"%PDF-1.4 sample", content_type="application/pdf")
    serializer = UploadResumeSerializer(data={"file": pdf})

    assert serializer.is_valid(), serializer.errors


@pytest.mark.happy_path
def test_scan_report_and_resume_history_serializers_output_expected_shape(
    sample_scan_report,
):
    """
    GIVEN a completed ScanReport nested inside a Resume (`scans` relation)
    WHEN  ScanReportSerializer and ResumeHistorySerializer serialize the objects
    THEN  all expected API/OpenAPI contract keys (`file_name`, `category_scores`,
          `section_critiques`, `scan_reports`) are properly mapped.
    """
    # 1. Direct ScanReport serialization
    report_data = ScanReportSerializer(sample_scan_report).data
    assert report_data["id"] == "11111111-1111-1111-1111-111111111111"
    assert report_data["file_name"] == "backend_resume.pdf"
    assert report_data["overall_score"] == 88
    assert report_data["category_scores"] == {"technical_skills": 92, "experience": 84}
    assert report_data["missing_keywords"] == ["Kubernetes"]
    assert report_data["section_critiques"][0]["section"] == "Experience"
    assert report_data["status"] == ScanReport.Status.DONE

    # 2. Nested ResumeHistory serialization (`source="scans"` -> `scan_reports`)
    resume_obj = SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        file_name="backend_resume.pdf",
        created_at=datetime(2026, 11, 22, 11, 55, tzinfo=timezone.utc),
        scans=[sample_scan_report],
    )
    history_data = ResumeHistorySerializer(resume_obj).data
    assert history_data["id"] == "22222222-2222-2222-2222-222222222222"
    assert history_data["file_name"] == "backend_resume.pdf"
    assert len(history_data["scan_reports"]) == 1
    assert history_data["scan_reports"][0]["id"] == "11111111-1111-1111-1111-111111111111"