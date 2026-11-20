from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from unfold.admin import ModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from api.models import CreditTransaction, Resume, ScanReport, User


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm

    ordering = ("email",)
    list_display = (
        "email",
        "first_name",
        "last_name",
        "credits",
        "is_staff",
        "created_at",
    )
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("email", "first_name", "last_name")
    readonly_fields = ("created_at", "last_login", "date_joined")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "credits")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined", "created_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "credits", "is_staff"),
            },
        ),
    )


@admin.register(Resume)
class ResumeAdmin(ModelAdmin):
    list_display = ("id", "user", "file_name", "created_at")
    search_fields = ("file_name", "user__email")
    list_filter = ("created_at",)
    readonly_fields = ("created_at",)


@admin.register(ScanReport)
class ScanReportAdmin(ModelAdmin):
    list_display = (
        "id",
        "resume",
        "target_job_title",
        "status",
        "overall_score",
        "created_at",
    )
    search_fields = ("target_job_title", "resume__file_name")
    list_filter = ("status", "created_at")
    readonly_fields = ("created_at",)


@admin.register(CreditTransaction)
class CreditTransactionAdmin(ModelAdmin):
    list_display = (
        "id",
        "user",
        "credits_added",
        "amount_cents",
        "description",
        "stripe_pay_id",
        "created_at",
    )
    search_fields = ("user__email", "description", "stripe_pay_id")
    list_filter = ("created_at",)
    readonly_fields = ("created_at",)
