import os

import redis
from allauth.socialaccount.providers.google.views import GoogleOAuth2Adapter
from allauth.socialaccount.providers.oauth2.client import OAuth2Client
from dj_rest_auth.registration.views import SocialLoginView
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Prefetch
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import generics, permissions, serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from api.models import Resume, ScanReport
from api.serializers import (
    LoginSerializer,
    RegisterSerializer,
    ResumeHistorySerializer,
    ScanReportSerializer,
    UploadResumeSerializer,
    UserMeSerializer,
    UserResponseSerializer,
)
from api.services import ResumeService
from api.throttles import ResumeUploadThrottle

User = get_user_model()

@csrf_exempt
@require_http_methods(["GET", "HEAD"])
def health_check(request):
    return JsonResponse({"status": "ok"}, status=200)

class BaseCustomSocialLoginView(SocialLoginView):
    permission_classes = (permissions.AllowAny,)

    def get_response(self):
        """
        Custom response structure matching standard login:
        Returns access token in JSON and attaches refresh token in HttpOnly cookie.
        """
        original_response = super().get_response()
        payload = (
            original_response.data
            if isinstance(original_response.data, dict)
            else {}
        )

        access_token = payload.get("access")
        refresh_token = payload.get("refresh")
        user_data = UserMeSerializer(self.user).data

        custom_response = Response(
            {
                "access": access_token,
                "user": user_data,
            },
            status=status.HTTP_200_OK,
        )

        if isinstance(refresh_token, str) and refresh_token:
            set_refresh_cookie(custom_response, refresh_token)

        return custom_response


class GoogleLoginAPIView(BaseCustomSocialLoginView):
    adapter_class = GoogleOAuth2Adapter
    client_class = OAuth2Client
    callback_url = f"{settings.FRONTEND_URL}/accounts/google/login/callback"

    @extend_schema(
        summary="Google OAuth2 Login",
        description="Accepts Google OAuth code/token and authenticates or registers user.",
        tags=["Authentication"],
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


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

    @extend_schema(
        tags=["Authentication"],
        operation_id="auth_register",
        summary="Register a new user",
        description="Creates a new user account, returns an access token, and sets the HttpOnly refresh cookie.",
    )
    def post(self, request, *args, **kwargs):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        saved_user = serializer.save()
        if isinstance(saved_user, list) or saved_user is None:
            raise ValueError("Expected a single user instance from RegisterSerializer.save().")

        refresh = RefreshToken.for_user(saved_user)
        user = saved_user

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

    @extend_schema(
        tags=["Authentication"],
        operation_id="auth_login",
        summary="Authenticate user and login",
        description="Validates credentials, returns an access token, and sets the HttpOnly refresh cookie.",
    )
    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        validated = (
            serializer.validated_data
            if isinstance(serializer.validated_data, dict)
            else {}
        )
        user = validated.get("user")
        if user is None:
            raise ValidationError("Unable to authenticate with provided credentials.")

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


class LogoutAPIView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    @extend_schema(
        summary="Logout User",
        tags=["Authentication"],
        responses={
            200: OpenApiResponse(description="Logout successful."),
        },
    )
    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get(settings.SIMPLE_JWT["AUTH_COOKIE"])

        if refresh_token:
            try:
                token = RefreshToken(refresh_token)
                token.blacklist()
            except TokenError:
                pass

        response = Response(
            {"message": "Logout successful."},
            status=status.HTTP_200_OK,
        )

        # Delete refresh cookie
        response.delete_cookie(
            key=settings.SIMPLE_JWT["AUTH_COOKIE"],
            path=settings.SIMPLE_JWT["AUTH_COOKIE_PATH"],
        )

        # Delete access cookie if configured separately
        access_cookie = settings.SIMPLE_JWT.get("AUTH_ACCESS_COOKIE")

        if access_cookie:
            response.delete_cookie(
                key=access_cookie,
                path=settings.SIMPLE_JWT.get("AUTH_ACCESS_COOKIE_PATH", "/"),
            )

        return response


class EmptySerializer(serializers.Serializer):
    pass


class TokenRefreshAPIView(APIView):
    permission_classes = (permissions.AllowAny,)
    serializer_class = EmptySerializer

    @extend_schema(
        tags=["Authentication"],
        operation_id="auth_token_refresh",
        summary="Refresh access token",
        description="Reads the HttpOnly refresh cookie and issues a new access token.",
    )
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


raw_redis_url = (os.getenv("REDIS_URL") or "redis://localhost:6379/0").strip()
if not raw_redis_url.startswith(("redis://", "rediss://", "unix://")):
    raw_redis_url = "redis://localhost:6379/0"
redis_client = redis.Redis.from_url(raw_redis_url)


class UploadResumeView(APIView):
    serializer_class = UploadResumeSerializer
    throttle_classes = (ResumeUploadThrottle,)

    @extend_schema(
        summary="Upload and Scan Resume",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "PDF or DOCX resume file",
                    },
                    "target_job_title": {
                        "type": "string",
                        "description": "Optional target job title",
                    },
                    "target_job_desc": {
                        "type": "string",
                        "description": "Optional target job description",
                    },
                },
                "required": ["file"],
            }
        },
        tags=["Resume Pipeline"],
    )
    def post(self, request, *args, **kwargs):
        user = request.user
        uploaded_file = request.FILES.get("file")

        if not uploaded_file:
            return Response(
                {"error": "No resume file provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_job_title = request.data.get("target_job_title", "General Software Role")
        target_job_desc = request.data.get(
            "target_job_desc", "Standard professional expectations."
        )

        try:
            # 1. Compute hash and extract text
            file_hash = ResumeService.compute_file_hash(uploaded_file)
            extracted_text = ResumeService.extract_resume_text(uploaded_file)

            # 2. Process upload (handles saving/updating resume and always triggers a fresh scan)
            resume, scan_report = ResumeService.process_resume_upload(
                user=user,
                uploaded_file=uploaded_file,
                file_hash=file_hash,
                extracted_text=extracted_text,
                job_title=target_job_title,
                job_desc=target_job_desc,
            )

            return Response(
                {
                    "message": "Resume uploaded successfully. Analysis in progress.",
                    "resume_id": resume.id,
                    "scan_id": str(scan_report.id),
                    "status": scan_report.status,
                    "remaining_credits": user.credits,
                },
                status=status.HTTP_201_CREATED,
            )

        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except APIException as e:
            return Response({"error": str(e)}, status=e.status_code)


class ScanResultDetailView(APIView):
    serializer_class = ScanReportSerializer

    @extend_schema(
        summary="Get Scan Report Results",
        description="Retrieves the AI analysis status and results for a given scan report ID.",
        responses={
            200: ScanReportSerializer,
            404: {"description": "Scan report not found."},
        },
        tags=["Resume Pipeline"],
    )
    def get(self, request, scan_id):
        scan_report = (
            ScanReport.objects.select_related("resume", "resume__user")
            .filter(id=scan_id, resume__user=request.user)
            .first()
        )
        if not scan_report:
            return Response(
                {"error": "Scan report not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ScanReportSerializer(scan_report)
        return Response(serializer.data, status=status.HTTP_200_OK)


@extend_schema(
    summary="Get User Scan History",
    description="Retrieves a list of resumes and their scan history for the authenticated user.",
    tags=["Resume Pipeline"],
)
class UserScanHistoryView(generics.ListAPIView):
    serializer_class = ResumeHistorySerializer
    permission_classes = (permissions.IsAuthenticated,)

    def get_queryset(self):
        scans_queryset = ScanReport.objects.order_by("-created_at")
        return (
            Resume.objects.filter(user=self.request.user)
            .prefetch_related(Prefetch("scans", queryset=scans_queryset))
            .order_by("-created_at")
        )

class UserCreditBalanceView(APIView):
    permission_classes = (permissions.IsAuthenticated,)
    serializer_class = UserMeSerializer

    @extend_schema(
        summary="Get current user's credit balance",
        responses={200: UserMeSerializer},
        tags=["Authentication"],
    )
    def get(self, request, *args, **kwargs):
        # Refresh from DB if needed so newly deducted/added credits are always accurate
        request.user.refresh_from_db(fields=["credits"])
        serializer = self.serializer_class(request.user, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)
