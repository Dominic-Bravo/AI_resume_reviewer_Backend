from django.urls import include, path
from rest_framework.routers import DefaultRouter

from api.views import (
    LoginAPIView,
    ProtectedTestView,
    RegisterAPIView,
    TokenRefreshAPIView,
)

router = DefaultRouter()

app_name = "users"

urlpatterns = [
    path("register/", RegisterAPIView.as_view(), name="auth_register"),
    path("login/", LoginAPIView.as_view(), name="login"),
    path("token/refresh/", TokenRefreshAPIView.as_view(), name="token_refresh"),
    path("api/v1/protected-test/", ProtectedTestView.as_view(), name="protected-test"),
    path("", include(router.urls)),
]
