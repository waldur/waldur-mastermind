from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms import ModelForm
from django.utils.translation import ugettext_lazy as _
from django.utils.translation import ungettext

from waldur_core.core import utils as core_utils
from waldur_core.core.admin import ExecutorAdminAction
from waldur_core.core.models import StateMixin
from waldur_core.structure import admin as structure_admin

from . import executors, tasks
from .models import (
    Allocation,
    AllocationUsage,
    AllocationUserUsage,
    SlurmService,
    SlurmServiceProjectLink,
)


def get_allocation_count(self, scope):
    return scope.quotas.get(name='nc_allocation_count').usage


get_allocation_count.short_description = _('Allocation count')

for cls in (structure_admin.CustomerAdmin, structure_admin.ProjectAdmin):
    cls.get_allocation_count = get_allocation_count
    cls.list_display += ('get_allocation_count',)


class AllocationAdmin(structure_admin.ResourceAdmin):
    class SyncAllocations(ExecutorAdminAction):
        executor = executors.AllocationPullExecutor
        short_description = _('Sync selected allocations')

        def validate(self, allocation):
            if allocation.state != StateMixin.States.OK:
                raise ValidationError(_('Allocation has to be in OK state.'))

    sync_allocations = SyncAllocations()

    def sync_users(self, request, queryset):
        valid_state = StateMixin.States.OK
        valid_allocations = queryset.filter(state=valid_state)
        for allocation in valid_allocations:
            serialized_allocation = core_utils.serialize_instance(allocation)
            tasks.add_allocation_users.delay(serialized_allocation)

        count = valid_allocations.count()
        message = ungettext(
            'One allocation users have been synchronized.',
            '%(count)d allocations users have been synchronized.',
            count,
        )
        message = message % {'count': count}

        self.message_user(request, message)

    sync_users.short_description = _('Synchronize allocation users')
    actions = ['sync_allocations', 'sync_users']


class AllocationUsageAdminForm(ModelForm):
    class Meta:
        labels = {'ram_usage': 'RAM usage (MB)'}


class AllocationUsageAdmin(admin.ModelAdmin):
    form = AllocationUsageAdminForm
    list_display = admin.ModelAdmin.list_display + (
        'allocation',
        'cpu_usage',
        'gpu_usage',
        'ram_usage_mb',
        'deposit_usage',
        'year',
        'month',
    )

    search_fields = ('allocation__name',)

    list_filter = ('allocation', 'year', 'month')

    def ram_usage_mb(self, obj):
        return obj.ram_usage

    ram_usage_mb.short_description = 'RAM usage (MB)'


class AllocationUserUsageAdmin(admin.ModelAdmin):
    list_display = admin.ModelAdmin.list_display + (
        'allocation_usage',
        'user',
        'username',
    )


admin.site.register(SlurmService, structure_admin.ServiceAdmin)
admin.site.register(SlurmServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
admin.site.register(Allocation, AllocationAdmin)
admin.site.register(AllocationUsage, AllocationUsageAdmin)
admin.site.register(AllocationUserUsage, AllocationUserUsageAdmin)
