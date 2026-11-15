import redis
import requests
from django.conf import settings
from django.middleware.csrf import get_token
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import permissions, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.serializers import CharField, Serializer
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from api.models import Resume, ScanReport, User
from api.serializers import (
    LoginSerializer,
    LogoutSerializer,
    RegisterSerializer,
    ScanReportSerializer,
    UploadResumeSerializer,
    UserMeSerializer,
    UserResponseSerializer,
)
from api.tasks import run_ai_review_task
from api.utils import calculate_file_hash


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


# Redis client for concurrency locking
redis_client = redis.Redis.from_url(
    getattr(settings, "REDIS_URL", "redis://redis:6379/0")
)


class UploadResumeView(APIView):
    serializer_class = UploadResumeSerializer

    @extend_schema(
        summary="Upload and Scan Resume (Idempotent)",
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
    def post(self, request):
        file_obj = request.FILES.get("file")
        target_job_title = request.data.get("target_job_title", "")
        target_job_desc = request.data.get("target_job_desc", "")

        if not file_obj:
            return Response(
                {"error": "No resume file provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Temporary mock user or request.user if using auth
        user, _ = User.objects.get_or_create(
            email="test@example.com", defaults={"password": "mock_password"}
        )

        # 1. Compute file hash
        file_hash = calculate_file_hash(file_obj)

        # 2. Redis Distributed Lock (prevents rapid multi-click duplicates)
        lock_key = f"lock:upload:{user.id}:{file_hash}"
        acquired = redis_client.set(lock_key, "locked", ex=10, nx=True)

        if not acquired:
            return Response(
                {
                    "error": "A duplicate upload request for this file is already being processed."
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            # 3. Persistent Database Idempotency Check
            existing_resume = Resume.objects.filter(
                user=user, file_hash=file_hash
            ).first()

            if existing_resume:
                scan_report, created = ScanReport.objects.get_or_create(
                    resume=existing_resume,
                    target_job_title=target_job_title,
                    defaults={"status": ScanReport.Status.PENDING},
                )

                return Response(
                    {
                        "message": "Idempotent hit: Resume already exists. No credits deducted.",
                        "resume_id": str(existing_resume.id),
                        "scan_id": str(scan_report.id),
                        "status": scan_report.status,
                        "cached": True,
                    },
                    status=status.HTTP_200_OK,
                )

            # 4. Check User Credits for Brand New Uploads
            if user.credits <= 0:
                return Response(
                    {
                        "error": "Insufficient credits. Please top up to analyze new resumes."
                    },
                    status=status.HTTP_402_PAYMENT_REQUIRED,
                )

            # 5. Forward to FastAPI for Parsing if brand new
            fastapi_url = "http://fastapi:8001/api/v1/parse"
            file_obj.seek(0)
            files = {"file": (file_obj.name, file_obj.read(), file_obj.content_type)}

            response = requests.post(fastapi_url, files=files, timeout=15)
            if response.status_code != 200:
                return Response(
                    {"error": "FastAPI parsing failed."},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            parse_data = response.json()

            # 6. Save to RESUMES table
            resume = Resume.objects.create(
                user=user,
                file_name=file_obj.name,
                file_hash=file_hash,
                parsed_text=parse_data.get("parsed_text", ""),
            )

            # 7. Create initial SCAN_REPORTS record
            scan_report = ScanReport.objects.create(
                resume=resume,
                target_job_title=target_job_title,
                target_job_desc=target_job_desc,
                status=ScanReport.Status.PENDING,
            )

            # 8. Deduct 1 credit for new successful analysis
            user.credits -= 1
            user.save()

            # 9. Dispatch Celery background task for AI analysis
            run_ai_review_task.delay(str(scan_report.id))

            return Response(
                {
                    "message": "Resume uploaded, parsed, and scan initiated successfully. 1 credit deducted.",
                    "resume_id": str(resume.id),
                    "scan_id": str(scan_report.id),
                    "status": scan_report.status,
                    "remaining_credits": user.credits,
                    "cached": False,
                },
                status=status.HTTP_201_CREATED,
            )

        finally:
            # Always release the Redis lock
            redis_client.delete(lock_key)


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
        try:
            scan_report = ScanReport.objects.get(id=scan_id)
        except ScanReport.DoesNotExist:
            return Response(
                {"error": "Scan report not found."},
                status=status.HTTP_404_NOT_FILE_FOUND
                if hasattr(status, "HTTP_404_NOT_FILE_FOUND")
                else status.HTTP_404_NOT_FOUND,
            )

        serializer = ScanReportSerializer(scan_report)
        return Response(serializer.data, status=status.HTTP_200_OK)
