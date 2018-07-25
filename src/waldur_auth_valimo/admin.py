from __future__ import unicode_literals

from django.contrib import admin

from . import models


class AuthResultAdmin(admin.ModelAdmin):
    list_display = ('uuid', 'state', 'user', 'modified')
    ordering = ('modified',)
    list_filter = ('state', 'user')


admin.site.register(models.AuthResult, AuthResultAdmin)
