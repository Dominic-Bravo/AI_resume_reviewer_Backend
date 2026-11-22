"""
Unit tests for `api.views` (Authentication & Resume Pipeline endpoints).

HOW TO MAINTAIN THIS FILE WHEN ADDING FEATURES:
-------------------------------------------------------------------------------
1. Added a new endpoint in `api/views.py`?
   -> Instantiate `APIRequestFactory()`, call `YourView.as_view()(request)`,
      and mock any serializer/service/ORM calls inside `api.views`.

2. Changed `UploadResumeView` response payload or error status codes?
   -> Update assertions under Section 3 (Resume Pipeline Views).

3. Changed JWT cookie settings or auth response format?
   -> Update assertions under Section 2 (Authentication & User Views).
-------------------------------------------------------------------------------
"""

from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from api.models import ScanReport
from api.throttles import ResumeUploadThrottle
from api.views import (
    LoginAPIView,
    LogoutAPIView,
    RegisterAPIView,
    ScanResultDetailView,
    TokenRefreshAPIView,
    UploadResumeView,
    UserCreditBalanceView,
    UserScanHistoryView,
    health_check,
)

# =============================================================================
# 1. FIXTURES (Request Factory, Mock User & Throttle Bypass)
# =============================================================================


@pytest.fixture
def rf():
    """DRF APIRequestFactory for invoking views directly without URL routing."""
    return APIRequestFactory()


@pytest.fixture
def mock_user(mocker):
    """Creates an authenticated in-memory user without touching the database."""
    user = mocker.MagicMock()
    user.id = 101
    user.pk = 101
    user.email = "engineer@example.com"
    user.credits = 4
    user.is_authenticated = True
    return user


@pytest.fixture
def bypass_upload_throttle(mocker):
    """Prevents ResumeUploadThrottle from hitting Redis/cache during unit tests."""
    return mocker.patch.object(ResumeUploadThrottle, "allow_request", return_value=True)


# =============================================================================
# 2. SYSTEM & AUTHENTICATION VIEWS
# =============================================================================


@pytest.mark.happy_path
def test_health_check_returns_200_ok(rf):
    """
    GIVEN a GET request to health_check
    WHEN  the view is called
    THEN  it returns 200 OK with `{"status": "ok"}`.
    """
    request = rf.get("/api/health/")
    response = health_check(request)

    assert response.status_code == 200
    assert response.content == b'{"status": "ok"}'


@pytest.mark.happy_path
def test_register_api_view_creates_user_and_sets_refresh_cookie(mocker, rf, mock_user):
    """
    GIVEN valid registration payload
    WHEN  RegisterAPIView handles POST
    THEN  it returns 201 Created with access token, user payload, and HttpOnly refresh cookie.
    """
    # Mock serializer validation & save
    mock_serializer = mocker.MagicMock()
    mock_serializer.save.return_value = mock_user
    mocker.patch("api.views.RegisterSerializer", return_value=mock_serializer)
    mocker.patch(
        "api.views.UserResponseSerializer",
        return_value=SimpleNamespace(data={"id": 101, "email": "engineer@example.com"}),
    )

    # Mock JWT RefreshToken.for_user
    mock_refresh = mocker.MagicMock()
    mock_refresh.__str__.return_value = "mock_refresh_jwt"
    mock_refresh.access_token = "mock_access_jwt"
    mocker.patch("api.views.RefreshToken.for_user", return_value=mock_refresh)

    request = rf.post("/api/auth/register/", {"email": "engineer@example.com"}, format="json")
    response = RegisterAPIView.as_view()(request)

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["access"] == "mock_access_jwt"
    assert response.data["user"]["email"] == "engineer@example.com"
    cookie_name = settings.SIMPLE_JWT["AUTH_COOKIE"]
    assert response.cookies[cookie_name].value == "mock_refresh_jwt"
    assert response.cookies[cookie_name]["httponly"] is True


@pytest.mark.happy_path
def test_login_api_view_authenticates_and_sets_cookie(mocker, rf, mock_user):
    """
    GIVEN valid login credentials
    WHEN  LoginAPIView handles POST
    THEN  it returns 200 OK with access token, serialized user, and refresh cookie.
    """
    mock_serializer = mocker.MagicMock()
    mock_serializer.validated_data = {"user": mock_user}
    mocker.patch("api.views.LoginSerializer", return_value=mock_serializer)
    mocker.patch(
        "api.views.UserMeSerializer",
        return_value=SimpleNamespace(data={"id": 101, "credits": 4}),
    )

    mock_refresh = mocker.MagicMock()
    mock_refresh.__str__.return_value = "login_refresh_jwt"
    mock_refresh.access_token = "login_access_jwt"
    mocker.patch("api.views.RefreshToken.for_user", return_value=mock_refresh)

    request = rf.post("/api/auth/login/", {"email": "engineer@example.com"}, format="json")
    response = LoginAPIView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    assert response.data["access"] == "login_access_jwt"
    assert response.data["user"]["credits"] == 4
    cookie_name = settings.SIMPLE_JWT["AUTH_COOKIE"]
    assert response.cookies[cookie_name].value == "login_refresh_jwt"


@pytest.mark.edge_case
def test_token_refresh_missing_cookie_returns_401(rf):
    """
    GIVEN a POST request to TokenRefreshAPIView without a refresh cookie
    WHEN  the view executes
    THEN  it returns 401 Unauthorized.
    """
    request = rf.post("/api/auth/refresh/")
    response = TokenRefreshAPIView.as_view()(request)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert "missing" in response.data["detail"].lower()


@pytest.mark.happy_path
def test_logout_blacklists_token_and_deletes_cookie(mocker, rf, mock_user):
    """
    GIVEN an authenticated user with a refresh token cookie
    WHEN  LogoutAPIView handles POST
    THEN  it blacklists the refresh token and clears the auth cookie.
    """
    mock_token_cls = mocker.patch("api.views.RefreshToken")
    cookie_name = settings.SIMPLE_JWT["AUTH_COOKIE"]

    request = rf.post("/api/auth/logout/")
    request.COOKIES[cookie_name] = "valid_refresh_token"
    force_authenticate(request, user=mock_user)

    response = LogoutAPIView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    mock_token_cls.assert_called_once_with("valid_refresh_token")
    mock_token_cls.return_value.blacklist.assert_called_once()
    assert response.cookies[cookie_name].value == ""


@pytest.mark.happy_path
def test_user_credit_balance_view_refreshes_and_returns_user(mocker, rf, mock_user):
    """
    GIVEN an authenticated user
    WHEN  GET UserCreditBalanceView is called
    THEN  it calls `refresh_from_db(fields=["credits"])` and returns serialized data.
    """
    mocker.patch(
        "api.views.UserMeSerializer",
        return_value=SimpleNamespace(data={"id": 101, "credits": 4}),
    )

    request = rf.get("/api/auth/credits/")
    force_authenticate(request, user=mock_user)

    response = UserCreditBalanceView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    mock_user.refresh_from_db.assert_called_once_with(fields=["credits"])
    assert response.data["credits"] == 4


# =============================================================================
# 3. RESUME PIPELINE VIEWS
# =============================================================================


@pytest.mark.happy_path
def test_upload_resume_view_success_returns_201(
    mocker, rf, mock_user, bypass_upload_throttle
):
    """
    GIVEN an authenticated user uploading a valid PDF file with target job info
    WHEN  UploadResumeView handles POST
    THEN  it computes hash, extracts text, processes upload, and returns 201 Created.
    """
    mocker.patch("api.views.ResumeService.compute_file_hash", return_value="sha256_hash")
    mocker.patch("api.views.ResumeService.extract_resume_text", return_value="Parsed text")

    mock_resume = SimpleNamespace(id=12)
    mock_scan = SimpleNamespace(id=88, status=ScanReport.Status.PENDING)
    mock_process = mocker.patch(
        "api.views.ResumeService.process_resume_upload",
        return_value=(mock_resume, mock_scan),
    )

    pdf = SimpleUploadedFile("cv.pdf", b"%PDF-1.4 content", content_type="application/pdf")
    request = rf.post(
        "/api/resumes/upload/",
        {"file": pdf, "target_job_title": "Backend Dev", "target_job_desc": "Django"},
        format="multipart",
    )
    force_authenticate(request, user=mock_user)

    response = UploadResumeView.as_view()(request)

    assert response.status_code == status.HTTP_201_CREATED
    assert response.data["resume_id"] == 12
    assert response.data["scan_id"] == "88"
    assert response.data["status"] == ScanReport.Status.PENDING
    assert response.data["remaining_credits"] == 4
    mock_process.assert_called_once()


@pytest.mark.edge_case
def test_upload_resume_view_missing_file_returns_400(rf, mock_user, bypass_upload_throttle):
    """
    GIVEN a multipart POST with no `file` field
    WHEN  UploadResumeView executes
    THEN  it immediately returns 400 Bad Request.
    """
    request = rf.post("/api/resumes/upload/", {}, format="multipart")
    force_authenticate(request, user=mock_user)

    response = UploadResumeView.as_view()(request)

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.data["error"] == "No resume file provided."


@pytest.mark.edge_case
def test_upload_resume_view_propagates_service_errors(
    mocker, rf, mock_user, bypass_upload_throttle
):
    """
    GIVEN ResumeService raises ValidationError (400) or APIException (e.g., 402 insufficient credits)
    WHEN  UploadResumeView catches the exception
    THEN  it returns the matching HTTP status code and error message.
    """
    pdf = SimpleUploadedFile("cv.pdf", b"%PDF-1.4 content", content_type="application/pdf")

    # 1. ValidationError -> 400
    mocker.patch("api.views.ResumeService.compute_file_hash", return_value="hash")
    mocker.patch(
        "api.views.ResumeService.extract_resume_text",
        side_effect=ValidationError("Corrupt PDF"),
    )
    req_400 = rf.post("/api/resumes/upload/", {"file": pdf}, format="multipart")
    force_authenticate(req_400, user=mock_user)
    res_400 = UploadResumeView.as_view()(req_400)
    assert res_400.status_code == status.HTTP_400_BAD_REQUEST

    # 2. APIException(code=402) -> 402
    pdf.seek(0)
    mocker.patch("api.views.ResumeService.extract_resume_text", return_value="Valid text")
    insufficient_exc = APIException("Insufficient credits")
    insufficient_exc.status_code = 402
    mocker.patch(
        "api.views.ResumeService.process_resume_upload",
        side_effect=insufficient_exc,
    )
    req_402 = rf.post("/api/resumes/upload/", {"file": pdf}, format="multipart")
    force_authenticate(req_402, user=mock_user)
    res_402 = UploadResumeView.as_view()(req_402)
    assert res_402.status_code == 402


@pytest.mark.happy_path
def test_scan_result_detail_view_returns_report_or_404(mocker, rf, mock_user):
    """
    GIVEN a scan_id owned by the user (or missing/owned by someone else)
    WHEN  GET ScanResultDetailView is called
    THEN  it returns 200 with serialized report data, or 404 if not found.
    """
    mock_report = SimpleNamespace(id=55, status=ScanReport.Status.DONE)
    first_mock = mocker.MagicMock(side_effect=[mock_report, None])
    mocker.patch(
        "api.views.ScanReport.objects.select_related",
        return_value=mocker.MagicMock(
            filter=mocker.MagicMock(return_value=mocker.MagicMock(first=first_mock))
        ),
    )
    mocker.patch(
        "api.views.ScanReportSerializer",
        return_value=SimpleNamespace(data={"id": 55, "status": "DONE", "overall_score": 91}),
    )

    # 1. Found -> 200
    req_ok = rf.get("/api/scans/55/")
    force_authenticate(req_ok, user=mock_user)
    res_ok = ScanResultDetailView.as_view()(req_ok, scan_id=55)
    assert res_ok.status_code == status.HTTP_200_OK
    assert res_ok.data["overall_score"] == 91

    # 2. Not found / unauthorized owner -> 404
    req_missing = rf.get("/api/scans/999/")
    force_authenticate(req_missing, user=mock_user)
    res_missing = ScanResultDetailView.as_view()(req_missing, scan_id=999)
    assert res_missing.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.happy_path
def test_user_scan_history_view_returns_user_resumes(mocker, rf, mock_user):
    """
    GIVEN an authenticated user with historical resumes
    WHEN  GET UserScanHistoryView is called
    THEN  it queries `Resume.objects.filter(user=request.user)` and returns 200 OK.
    """
    mock_qs = mocker.MagicMock()
    mock_qs.prefetch_related.return_value.order_by.return_value = [
        SimpleNamespace(id=1, file_name="resume_v1.pdf")
    ]
    filter_mock = mocker.patch("api.views.Resume.objects.filter", return_value=mock_qs)
    mocker.patch(
        "api.views.ResumeHistorySerializer",
        return_value=SimpleNamespace(data=[{"id": 1, "file_name": "resume_v1.pdf"}]),
    )

    request = rf.get("/api/scans/history/")
    force_authenticate(request, user=mock_user)

    response = UserScanHistoryView.as_view()(request)

    assert response.status_code == status.HTTP_200_OK
    filter_mock.assert_called_once_with(user=mock_user)
    assert response.data[0]["file_name"] == "resume_v1.pdf"