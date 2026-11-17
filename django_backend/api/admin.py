from django.contrib import admin

from api.models import CreditTransaction, Resume, ScanReport, User

# Register your models here.
admin.site.register(User)
admin.site.register(Resume)
admin.site.register(ScanReport)
admin.site.register(CreditTransaction)
