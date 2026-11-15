from django.conf import settings
from django.middleware.csrf import get_token
from rest_framework import permissions, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from api.serializers import (
    LoginSerializer,
    LogoutSerializer,
    RegisterSerializer,
    UserMeSerializer,
    UserResponseSerializer,
)


def set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Attaches HttpOnly cookie to response."""
    response.set_cookie(
        key=settings.SIMPLE_JWT["AUTH_COOKIE"],
        value=refresh_token,
        max_age=int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
        httponly=settings.SIMPLE_JWT["AUTH_COOKIE_HTTP_ONLY"],
        secure=settings.SIMPLE_JWT["AUTH_COOKIE_SECURE"],
        samesite=settings.SIMPLE_JWT["AUTH_COOKIE_SAMESITE"],
        path=settings.SIMPLE_JWT["AUTH_COOKIE_PATH"],
    )


class RegisterAPIView(APIView):
    permission_classes = (permissions.AllowAny,)
    serializer_class = RegisterSerializer

    def post(self, request, *args, **kwargs):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = serializer.save()
        refresh = RefreshToken.for_user(user)

        return Response(
            {
                "user": UserResponseSerializer(user).data,
                "tokens": {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                },
                "message": "User registered successfully.",
            },
            status=status.HTTP_201_CREATED,
        )


class LoginAPIView(APIView):
    permission_classes = (permissions.AllowAny,)
    serializer_class = LoginSerializer

    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        user = serializer.validated_data["user"]
        refresh = RefreshToken.for_user(user)

        response = Response(
            {
                "access": str(refresh.access_token),
                "user": UserMeSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )

        set_refresh_cookie(response, str(refresh))
        get_token(request)

        return response


class TokenRefreshAPIView(APIView):
    permission_classes = (permissions.AllowAny,)
    serializer_class = LogoutSerializer  # Fallback serializer for docs

    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get(settings.SIMPLE_JWT["AUTH_COOKIE"])

        if not refresh_token:
            return Response(
                {"detail": "Refresh token missing from cookie."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            refresh = RefreshToken(refresh_token)
            data = {"access": str(refresh.access_token)}

            if settings.SIMPLE_JWT.get("ROTATE_REFRESH_TOKENS", False):
                refresh.set_jti()
                refresh.set_exp()
                new_refresh = str(refresh)

            response = Response(data, status=status.HTTP_200_OK)

            if settings.SIMPLE_JWT.get("ROTATE_REFRESH_TOKENS", False):
                set_refresh_cookie(response, new_refresh)

            return response
        except TokenError:
            return Response(
                {"detail": "Invalid or expired refresh token."},
                status=status.HTTP_401_UNAUTHORIZED,
            )


class ProtectedTestView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        # request.user is automatically populated by your JWT authentication backend
        user = request.user
        return Response(
            {
                "message": "Authorization successful! You have accessed a protected endpoint.",
                "user_id": str(user.id),
                "email": user.email,
                "credits": user.credits,
            },
            status=status.HTTP_200_OK,
        )
