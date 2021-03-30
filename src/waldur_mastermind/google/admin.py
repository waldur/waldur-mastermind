from django.contrib import admin

from . import models


class GoogleCalendarAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'http_link')


admin.site.register(models.GoogleCalendar, GoogleCalendarAdmin)
