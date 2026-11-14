from django.urls import include, path
from rest_framework.routers import DefaultRouter

from api.views import LoginAPIView, RegisterAPIView, TokenRefreshAPIView

router = DefaultRouter()

app_name = "users"

urlpatterns = [
    path("register/", RegisterAPIView.as_view(), name="auth_register"),
    path("login/", LoginAPIView.as_view(), name="login"),
    path("token/refresh/", TokenRefreshAPIView.as_view(), name="token_refresh"),
    path("", include(router.urls)),
]
