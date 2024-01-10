from django.contrib import admin

from . import models


class IdentityProviderAdmin(admin.ModelAdmin):
    fields = ("name", "url")
    readonly_fields = ("name", "url")
    list_display = ("name",)
    search_fields = ("name",)


admin.site.register(models.IdentityProvider, IdentityProviderAdmin)
