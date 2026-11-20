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


@admin.register(Resume)
class ResumeAdmin(ModelAdmin):
    list_display = ("id", "user", "file_name", "created_at")
    search_fields = ("file_name", "user__email")
    list_filter = ("created_at",)


@admin.register(ScanReport)
class ScanReportAdmin(ModelAdmin):
    list_display = ("id", "resume", "target_job_title", "status", "overall_score", "created_at")
    search_fields = ("target_job_title", "resume__file_name")
    list_filter = ("status", "created_at")


@admin.register(CreditTransaction)
class CreditTransactionAdmin(ModelAdmin):
    list_display = ("id", "user", "amount", "transaction_type", "created_at")
    search_fields = ("user__email",)
    list_filter = ("transaction_type", "created_at")