from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from waldur_core.core import utils as core_utils
from waldur_core.core.admin import ExecutorAdminAction
from waldur_core.core.models import StateMixin
from waldur_core.structure import admin as structure_admin

from . import executors, models, tasks
from .models import Allocation, AllocationUserUsage, Association


def get_allocation_count(self, scope):
    return scope.get_quota_usage("op_allocation_count")


get_allocation_count.short_description = _("Allocation count")

for cls in (structure_admin.CustomerAdmin, structure_admin.ProjectAdmin):
    cls.get_allocation_count = get_allocation_count
    cls.list_display += ("get_allocation_count",)


class AllocationAdmin(structure_admin.ResourceAdmin):
    class SyncAllocations(ExecutorAdminAction):
        executor = executors.AllocationPullExecutor
        short_description = _("Sync selected allocations")

        def validate(self, allocation):
            if allocation.state not in [StateMixin.States.OK, StateMixin.States.ERRED]:
                raise ValidationError(_("Allocation has to be in OK or ERRED state."))

    sync_allocations = SyncAllocations()

    def sync_users(self, request, queryset):
        valid_state = StateMixin.States.OK
        valid_allocations = queryset.filter(state=valid_state)
        for allocation in valid_allocations:
            serialized_allocation = core_utils.serialize_instance(allocation)
            tasks.sync_allocation_users.delay(serialized_allocation)

        count = valid_allocations.count()
        message = ngettext(
            "One allocation users have been synchronized.",
            "%(count)d allocations users have been synchronized.",
            count,
        )
        message = message % {"count": count}

        self.message_user(request, message)

    sync_users.short_description = _("Synchronize allocation users")
    actions = ["sync_allocations", "sync_users"]


class AllocationUserUsageAdmin(admin.ModelAdmin):
    list_display = admin.ModelAdmin.list_display + (
        "allocation",
        "user",
        "year",
        "month",
    )


class AssociationAdmin(admin.ModelAdmin):
    list_display = admin.ModelAdmin.list_display + (
        "allocation",
        "username",
    )


admin.site.register(Allocation, AllocationAdmin)
admin.site.register(AllocationUserUsage, AllocationUserUsageAdmin)
admin.site.register(Association, AssociationAdmin)


class RemoteProjectAuditEntryInline(admin.TabularInline):
    model = models.RemoteProjectAuditEntry
    extra = 0
    readonly_fields = (
        "timestamp",
        "event_type",
        "performed_by",
        "note",
        "remote_response",
    )
    fields = readonly_fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class RemoteProjectAllocationEntryInline(admin.TabularInline):
    model = models.RemoteProjectAllocationEntry
    extra = 0
    readonly_fields = (
        "submitted_at",
        "confirmed_at",
        "allocation",
        "previous_allocation",
        "source_project",
        "note",
    )
    fields = readonly_fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class RemoteProjectAdmin(admin.ModelAdmin):
    list_display = (
        "identifier",
        "destination",
        "state",
        "current_project",
        "current_allocation",
        "last_contact_time",
        "created",
    )
    list_filter = ("state",)
    search_fields = ("identifier", "destination")
    readonly_fields = (
        "uuid",
        "created",
        "modified",
        "last_contact_time",
        "last_sent_details",
        "last_confirmed_details",
    )
    fields = (
        "uuid",
        "destination",
        "identifier",
        "state",
        "current_project",
        "remote_allocation",
        "current_allocation",
        "pending_allocation",
        "pending_details",
        "pending_since",
        "last_sent_details",
        "last_confirmed_details",
        "last_contact_time",
        "created",
        "modified",
    )
    inlines = [
        RemoteProjectAllocationEntryInline,
        RemoteProjectAuditEntryInline,
    ]


class RemoteProjectAuditEntryAdmin(admin.ModelAdmin):
    list_display = (
        "remote_project",
        "event_type",
        "timestamp",
        "performed_by",
    )
    list_filter = ("event_type",)
    search_fields = (
        "remote_project__identifier",
        "remote_project__destination",
        "note",
    )
    readonly_fields = (
        "remote_project",
        "timestamp",
        "event_type",
        "previous_details",
        "new_details",
        "performed_by",
        "remote_response",
        "note",
        "allocation_entry",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class RemoteProjectAllocationEntryAdmin(admin.ModelAdmin):
    list_display = (
        "remote_project",
        "allocation",
        "previous_allocation",
        "submitted_at",
        "confirmed_at",
        "source_project",
    )
    search_fields = (
        "remote_project__identifier",
        "remote_project__destination",
    )
    readonly_fields = (
        "remote_project",
        "allocation",
        "previous_allocation",
        "attachment",
        "source_project",
        "submitted_at",
        "confirmed_at",
        "note",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(models.RemoteProject, RemoteProjectAdmin)
admin.site.register(models.RemoteProjectAuditEntry, RemoteProjectAuditEntryAdmin)
admin.site.register(
    models.RemoteProjectAllocationEntry,
    RemoteProjectAllocationEntryAdmin,
)
