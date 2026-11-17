import hashlib

import pypdf
import redis
import requests
from django.conf import settings
from django.middleware.csrf import get_token
from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from rest_framework import permissions, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.serializers import CharField, Serializer
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from api.models import CreditTransaction, Resume, ScanReport, User
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

    @extend_schema(
        tags=["Authentication"],
        operation_id="auth_register",
        summary="Register a new user",
        description="Creates a new user account, returns an access token, and sets the HttpOnly refresh cookie.",
    )
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

    @extend_schema(
        tags=["Authentication"],
        operation_id="auth_login",
        summary="Authenticate user and login",
        description="Validates credentials, returns an access token, and sets the HttpOnly refresh cookie.",
    )
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
    def post(self, request, *args, **kwargs):
        user = request.user
        uploaded_file = request.FILES.get("file")
        target_job_title = request.data.get("target_job_title", "General Software Role")
        target_job_desc = request.data.get(
            "target_job_desc", "Standard professional expectations."
        )

        if not uploaded_file:
            return Response(
                {"error": "No resume file provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 1. Compute file hash for idempotency checking
        file_bytes = uploaded_file.read()
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        uploaded_file.seek(0)  # Reset pointer after reading

        # 2. Check if this exact resume already exists for this user
        existing_resume = Resume.objects.filter(file_hash=file_hash, user=user).first()
        latest_scan = None

        if existing_resume:
            latest_scan = existing_resume.scans.order_by("-created_at").first()

            # If the previous scan failed, re-trigger the AI task without deducting credits again
            if latest_scan and latest_scan.status == ScanReport.Status.FAILED:
                latest_scan.status = ScanReport.Status.PENDING
                latest_scan.save(update_fields=["status"])
                run_ai_review_task.delay(latest_scan.id)

                return Response(
                    {
                        "message": "Previous scan failed. Retrying AI analysis...",
                        "resume_id": existing_resume.id,
                        "scan_id": latest_scan.id,
                        "status": "PENDING",
                    },
                    status=status.HTTP_200_OK,
                )

            # Normal idempotent hit for successful or pending scans
            return Response(
                {
                    "message": "Idempotent hit: Resume already exists. No credits deducted.",
                    "resume_id": existing_resume.id,
                    "scan_id": latest_scan.id if latest_scan else None,
                    "status": latest_scan.status if latest_scan else "UNKNOWN",
                    "cached": True,
                },
                status=status.HTTP_200_OK,
            )

        extracted_text = ""

        try:
            reader = pypdf.PdfReader(uploaded_file)
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    extracted_text += text + "\n"
        except Exception as e:
            return Response(
                {"error": f"FAILED to parse PDF text: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        extracted_text = extracted_text.strip()

        if not extracted_text:
            return Response(
                {
                    "error": "The uploaded PDF appears to be empty or contains no selectable text."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 3. Check credits before processing a brand new upload
        if user.credits < 1:
            return Response(
                {
                    "error": "Insufficient credits. Please top up to analyze new resumes."
                },
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )

        # Deduct credit & record transaction
        user.credits -= 1
        user.save()

        CreditTransaction.objects.create(
            user=user,
            credits_added=-1,  # Negative value indicates usage
            amount_cents=0,  # No monetary charge for using existing credits
            description=f"Resume scan deduction for {uploaded_file.name}",
        )
        # 5. Create Resume and initial pending ScanReport record
        # (Assuming your parser handles text extraction or stores the file properly)
        resume = Resume.objects.create(
            user=user,
            file_name=uploaded_file.name,
            file_hash=file_hash,
            parsed_text=extracted_text,
        )

        scan_report = ScanReport.objects.create(
            resume=resume,
            target_job_title=target_job_title,
            target_job_desc=target_job_desc,
            status=ScanReport.Status.PENDING,
        )

        # 6. Dispatch background Celery task for AI generation
        run_ai_review_task.delay(scan_report.id)

        return Response(
            {
                "message": "Resume uploaded successfully. Analysis in progress.",
                "resume_id": resume.id,
                "scan_id": scan_report.id,
                "status": scan_report.status,
                "remaining_credits": user.credits,
            },
            status=status.HTTP_201_CREATED,
        )


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
