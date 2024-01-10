from django.contrib import admin

from . import models


class GoogleCalendarAdmin(admin.ModelAdmin):
    list_display = ("__str__", "http_link")


class GoogleCredentialsAdmin(admin.ModelAdmin):
    list_display = ("service_provider",)


admin.site.register(models.GoogleCalendar, GoogleCalendarAdmin)
admin.site.register(models.GoogleCredentials, GoogleCredentialsAdmin)
