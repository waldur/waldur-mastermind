from django.contrib import admin

from . import models

admin.site.register(models.Issue, admin.ModelAdmin)
admin.site.register(models.Comment, admin.ModelAdmin)
admin.site.register(models.SupportUser, admin.ModelAdmin)
