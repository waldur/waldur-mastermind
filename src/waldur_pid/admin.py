from django.contrib import admin

from . import models


class DataciteReferralAdmin(admin.ModelAdmin):
    list_display = ('scope', 'pid', 'relation_type', 'publisher', 'published')
    list_filter = (
        'relation_type',
        'publisher',
    )
    search_fields = ('pid', 'publisher')


admin.site.register(models.DataciteReferral, DataciteReferralAdmin)
