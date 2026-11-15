from django.conf import settings
from django.middleware.csrf import get_token
from rest_framework import permissions, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
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

        response = Response(
            {
                "access": str(refresh.access_token),
                "user": UserResponseSerializer(user).data,
                "message": "User registered successfully.",
            },
            status=status.HTTP_201_CREATED,
        )

        # Store refresh token in HttpOnly cookie.
        set_refresh_cookie(response, str(refresh))

        return response


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
    serializer_class = LogoutSerializer

    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get(settings.SIMPLE_JWT["AUTH_COOKIE"])

        if not refresh_token:
            return Response(
                {"detail": "Refresh token missing from cookie."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            refresh = RefreshToken(refresh_token)

            # Generate a new access token.
            access_token = str(refresh.access_token)

            response = Response(
                {"access": access_token},
                status=status.HTTP_200_OK,
            )

            # Rotate refresh token when enabled.
            if api_settings.ROTATE_REFRESH_TOKENS:
                # Blacklist the old refresh token.
                if api_settings.BLACKLIST_AFTER_ROTATION:
                    refresh.blacklist()

                # Generate a new refresh token.
                refresh.set_jti()
                refresh.set_exp()
                refresh.set_iat()

                new_refresh = str(refresh)

                # Store the new refresh token in HttpOnly cookie.
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
