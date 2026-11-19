from django.urls import include, path
from rest_framework.routers import DefaultRouter

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
)

router = DefaultRouter()

app_name = "users"

urlpatterns = [
    path("register/", RegisterAPIView.as_view(), name="auth_register"),
    path("login/", LoginAPIView.as_view(), name="login"),
    path("token/refresh/", TokenRefreshAPIView.as_view(), name="token_refresh"),
    path("auth/google/", GoogleLoginAPIView.as_view(), name="google_login"),
    path("scans/history/", UserScanHistoryView.as_view(), name="scan-history"),
    path("logout/", LogoutAPIView.as_view(), name="logout"),
    path("api/v1/upload-resume/", UploadResumeView.as_view(), name="upload-resume"),
    path("credits/", UserCreditBalanceView.as_view(), name="user-credits"),
    path(
        "api/v1/scans/<uuid:scan_id>/",
        ScanResultDetailView.as_view(),
        name="scan-detail",
    ),
    path("", include(router.urls)),
]
