from django.contrib import admin

from waldur_mastermind.marketplace_remote import models


class RemoteLocalCategoryInline(admin.StackedInline):
    model = models.RemoteLocalCategory


class RemoteSynchronisationAdmin(admin.ModelAdmin):
    list_display = ("local_service_provider", "is_active", "get_state_display")
    inlines = [RemoteLocalCategoryInline]


admin.site.register(models.RemoteSynchronisation, RemoteSynchronisationAdmin)
