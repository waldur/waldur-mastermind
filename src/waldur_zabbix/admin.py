from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.utils.translation import ungettext
from waldur_core.core.admin import JsonWidget

from waldur_core.core.admin import ExecutorAdminAction
from waldur_core.core.tasks import send_task
from waldur_core.structure import admin as structure_admin
from . import executors
from .models import ZabbixServiceProjectLink, ZabbixService, Host, SlaHistory, SlaHistoryEvent, ITService


class SlaHistoryEventsInline(admin.TabularInline):
    model = SlaHistoryEvent
    fields = ('timestamp', 'state')
    ordering = ('timestamp', )
    extra = 1


class SlaHistoryAdmin(admin.ModelAdmin):
    inlines = (
        SlaHistoryEventsInline,
    )
    list_display = ('itservice', 'period', 'value')
    ordering = ('itservice', 'period')
    list_filter = ('period',)


class HostAdminForm(forms.ModelForm):
    class Meta:
        widgets = {
            'interface_parameters': JsonWidget(),
        }


class HostAdmin(structure_admin.ResourceAdmin):
    actions = ['pull_sla', 'pull']
    form = HostAdminForm

    # TODO: Rewrite with executor.
    def pull_sla(self, request, queryset):
        send_task('zabbix', 'pull_sla')([host.uuid.hex for host in queryset])

        tasks_scheduled = queryset.count()
        message = ungettext(
            'SLA pulling has been scheduled for one host',
            'SLA pulling has been scheduled for %(tasks_scheduled)d hosts',
            tasks_scheduled
        )
        message = message % {'tasks_scheduled': tasks_scheduled}

        self.message_user(request, message)

    pull_sla.short_description = "Pull SLAs for given Zabbix hosts"

    class Pull(ExecutorAdminAction):
        executor = executors.HostPullExecutor
        short_description = 'Pull'

        def validate(self, instance):
            if instance.state not in (Host.States.OK, Host.States.ERRED):
                raise ValidationError('Host has to be in stable state (OK or ERRED) to be pulled.')

    pull = Pull()


class ITServiceAdmin(structure_admin.ResourceAdmin):
    pass


admin.site.register(Host, HostAdmin)
admin.site.register(ITService, ITServiceAdmin)
admin.site.register(ZabbixService, structure_admin.ServiceAdmin)
admin.site.register(ZabbixServiceProjectLink, structure_admin.ServiceProjectLinkAdmin)
admin.site.register(SlaHistory, SlaHistoryAdmin)
