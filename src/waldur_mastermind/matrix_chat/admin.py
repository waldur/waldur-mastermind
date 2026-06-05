from django.contrib import admin

from . import models


class MatrixAppserviceTransactionAdmin(admin.ModelAdmin):
    list_display = ("txn_id", "event_count", "processed_at")
    search_fields = ("txn_id",)
    ordering = ("-processed_at",)


admin.site.register(
    models.MatrixAppserviceTransaction, MatrixAppserviceTransactionAdmin
)
