import uuid

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("The Email field must be set")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None  # Remove default username field in favor of email
    email = models.EmailField(unique=True, max_length=255)
    credits = models.IntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ()  # No extra fields required when running createsuperuser

    def __str__(self):
        return self.email


class Resume(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="resumes")
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64, db_index=True, blank=True, null=True)
    parsed_text = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "file_hash")


class ScanReport(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        FAILED = "FAILED", "Failed"
        DONE = "DONE", "Done"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resume = models.ForeignKey(Resume, on_delete=models.CASCADE, related_name="scans")
    target_job_title = models.CharField(max_length=255, blank=True, null=True)
    target_job_desc = models.TextField(blank=True, null=True)
    overall_score = models.IntegerField(default=0)
    category_scores = models.JSONField(default=dict, blank=True)
    missing_keywords = models.JSONField(default=list, blank=True)
    # Change bullet_critiques -> section_critiques to accurately reflect section-level feedback
    section_critiques = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)


class CreditTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="transactions"
    )
    stripe_pay_id = models.CharField(max_length=255, blank=True, null=True)
    credits_added = models.IntegerField(
        default=0
    )  # Positive for top-ups, negative for usage
    amount_cents = models.IntegerField(
        default=0
    )  # Cost in cents (0 for free/deductions)
    description = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.email} - {self.credits_added} credits ({self.description or 'Stripe Top-up'})"
