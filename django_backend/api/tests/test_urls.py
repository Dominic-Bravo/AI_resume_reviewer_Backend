"""
URL routing contract tests for `api.urls`.

HOW TO MAINTAIN THIS FILE WHEN ADDING FEATURES:
-------------------------------------------------------------------------------
1. Added a new endpoint in `api/urls.py`?
   -> Add a tuple `(url_name, kwargs, expected_path, expected_view)` to
      `ROUTE_CONTRACTS` below so frontend API paths and view bindings stay
      locked against accidental renames.
-------------------------------------------------------------------------------
"""

import pytest
from django.urls import Resolver404, resolve, reverse

from api.views import (
    GoogleLoginAPIView,
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

SAMPLE_UUID = "11111111-1111-1111-1111-111111111111"

# (namespaced_url_name, reverse_kwargs, expected_relative_path, expected_view_callable)
ROUTE_CONTRACTS = [
    ("users:auth_register", None, "/register/", RegisterAPIView),
    ("users:login", None, "/login/", LoginAPIView),
    ("users:token_refresh", None, "/token/refresh/", TokenRefreshAPIView),
    ("users:google_login", None, "/auth/google/", GoogleLoginAPIView),
    ("users:logout", None, "/logout/", LogoutAPIView),
    ("users:user-credits", None, "/credits/", UserCreditBalanceView),
    ("users:upload-resume", None, "/upload-resume/", UploadResumeView),
    ("users:scan-history", None, "/scans/history/", UserScanHistoryView),
    (
        "users:scan-detail",
        {"scan_id": SAMPLE_UUID},
        f"/scans/{SAMPLE_UUID}/",
        ScanResultDetailView,
    ),
    ("users:health_check", None, "/api/health/", health_check),
]


@pytest.mark.happy_path
@pytest.mark.parametrize(
    "url_name,kwargs,expected_suffix,expected_view",
    ROUTE_CONTRACTS,
)
def test_api_urls_reverse_and_resolve_to_expected_views(
    url_name, kwargs, expected_suffix, expected_view
):
    """
    GIVEN a named route in `api.urls` (`app_name = "users"`)
    WHEN  `reverse()` and `resolve()` are called
    THEN  the generated path ends with the expected route and maps to the exact view.
    """
    url_path = reverse(url_name, kwargs=kwargs)
    assert url_path.endswith(expected_suffix)

    match = resolve(url_path)
    # Class-based views expose `.view_class`; function views match directly
    resolved_target = getattr(match.func, "view_class", match.func)
    assert resolved_target is expected_view


@pytest.mark.edge_case
def test_scan_detail_rejects_non_uuid_scan_id():
    """
    GIVEN a path with an invalid non-UUID `scan_id` (e.g., `/scans/12345/`)
    WHEN  `resolve()` is called
    THEN  Django raises `Resolver404` because the route enforces `<uuid:scan_id>`.
    """
    base_history_path = reverse("users:scan-history")
    invalid_scan_path = base_history_path.replace("history/", "not-a-valid-uuid/")

    with pytest.raises(Resolver404):
        resolve(invalid_scan_path)