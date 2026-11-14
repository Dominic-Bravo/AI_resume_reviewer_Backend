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
        extra_fields.setdefault("role", User.Role.ADMIN)
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
    parsed_text = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class ScanReport(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        FAILED = "FAILED", "Failed"
        DONE = "DONE", "Done"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resume = models.ForeignKey(Resume, on_delete=models.CASCADE, related_name="scans")
    target_job_title = models.CharField(max_length=255, blank=True, null=True)
    target_job_desc = models.TextField(blank=True, null=True)
    overall_score = models.IntegerField(default=0)
    category_scores = models.JSONField(default=dict, blank=True)
    missing_keywords = models.JSONField(default=list, blank=True)
    bullet_critiques = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)


class CreditTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="transactions"
    )
    stripe_pay_id = models.CharField(max_length=255)
    credits_added = models.IntegerField()
    amount_cents = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
