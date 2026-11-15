from django.urls import include, path
from rest_framework.routers import DefaultRouter

from api.views import (
    LoginAPIView,
    RegisterAPIView,
    ScanResultDetailView,
    TokenRefreshAPIView,
    UploadResumeView,
)

router = DefaultRouter()

app_name = "users"

urlpatterns = [
    path("register/", RegisterAPIView.as_view(), name="auth_register"),
    path("login/", LoginAPIView.as_view(), name="login"),
    path("token/refresh/", TokenRefreshAPIView.as_view(), name="token_refresh"),
    path("api/v1/upload-resume/", UploadResumeView.as_view(), name="upload-resume"),
    path(
        "api/v1/scans/<uuid:scan_id>/",
        ScanResultDetailView.as_view(),
        name="scan-detail",
    ),
    path("", include(router.urls)),
]
