from django.contrib.auth import authenticate, get_user_model
from django.db import transaction
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from api.models import ScanReport

User = get_user_model()


# register user
class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        required=True,
        style={"input_type": "password"},
        min_length=8,
    )
    password_confirm = serializers.CharField(
        write_only=True,
        required=True,
        style={"input_type": "password"},
    )

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "password",
            "password_confirm",
        )

    def validate_email(self, value):
        normalized_email = value.lower().strip()
        if User.objects.filter(email=normalized_email).exists():
            raise serializers.ValidationError(
                "A user with this email address already exists."
            )
        return normalized_email

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError(
                {"password_confirm": "Passwords do not match."}
            )
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("password_confirm")
        password = validated_data.pop("password")

        # Create user instance
        user = User.objects.create_user(password=password, **validated_data)

        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(required=True)
    password = serializers.CharField(
        write_only=True,
        required=True,
        style={"input_type": "password"},
    )

    def validate(self, attrs):
        email = attrs.get("email", "").lower().strip()
        password = attrs.get("password")

        user = authenticate(
            request=self.context.get("request"),
            username=email,
            password=password,
        )

        if not user:
            raise serializers.ValidationError("Invalid email or password.")

        if not user.is_active:
            raise serializers.ValidationError("User account is disabled.")

        attrs["user"] = user
        return attrs


class UserResponseSerializer(serializers.ModelSerializer):
    """Clean representation serializer for outputting created user details."""

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "username",
            "credits",
            "created_at",
        )


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(
        required=True, help_text="The refresh token to blacklist."
    )


class UserMeSerializer(serializers.ModelSerializer):
    """Serializer for fetching the authenticated user's profile."""

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "username",
            "credits",
            "created_at",
        )
        read_only_fields = ("id", "email", "created_at")


class UploadResumeSerializer(serializers.Serializer):
    file = serializers.FileField(help_text="PDF or DOCX resume file")
    target_job_title = serializers.CharField(
        required=False, allow_blank=True, help_text="Optional target job title"
    )
    target_job_desc = serializers.CharField(
        required=False, allow_blank=True, help_text="Optional target job description"
    )


class ScanReportSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="resume.file_name", read_only=True)

    # 1. Change to SerializerMethodField so DRF calls the get_category_scores method
    category_scores = serializers.SerializerMethodField()

    missing_keywords = serializers.ListField(
        child=serializers.CharField(),
        help_text="List of missing keywords from the job description.",
    )
    bullet_critiques = serializers.ListField(
        child=serializers.CharField(),
        help_text="Actionable feedback for resume bullet points.",
    )

    class Meta:
        model = ScanReport
        fields = (
            "id",
            "file_name",
            "target_job_title",
            "target_job_desc",
            "overall_score",
            "category_scores",
            "missing_keywords",
            "bullet_critiques",
            "status",
            "created_at",
        )

    # 2. This explicitly dictates the exact JSON structure for Swagger & Orval
    @extend_schema_field(
        {
            "type": "object",
            "properties": {
                "experience": {"type": "integer"},
                "skills": {"type": "integer"},
                "formatting": {"type": "integer"},
                "relevance": {"type": "integer"},
            },
        }
    )
    def get_category_scores(self, obj):
        return obj.category_scores
