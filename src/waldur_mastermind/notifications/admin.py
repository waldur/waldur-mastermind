from django.contrib import admin

from . import models


class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ('name',)


admin.site.register(models.MessageTemplate, MessageTemplateAdmin)
