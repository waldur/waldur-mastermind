import logging

from django.contrib.contenttypes.models import ContentType
from django.core import exceptions as django_exceptions
from django.db import IntegrityError, transaction

from waldur_core.core.models import StateMixin
from waldur_core.quotas.models import Quota
from waldur_core.structure import models as structure_models

from ..openstack import apps as openstack_apps
from ..openstack import models as openstack_models
from . import apps, log, models

logger = logging.getLogger(__name__)


def _log_scheduled_action(resource, action, action_details):
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    log.event_logger.openstack_resource_action.info(
        'Operation "%s" has been scheduled for %s "%s"'
        % (message, class_name, resource.name),
        event_type=_get_action_event_type(action, 'scheduled'),
        event_context={'resource': resource, 'action_details': action_details},
    )


def _log_succeeded_action(resource, action, action_details):
    if not action:
        return
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    log.event_logger.openstack_resource_action.info(
        'Successfully executed "%s" operation for %s "%s"'
        % (message, class_name, resource.name),
        event_type=_get_action_event_type(action, 'succeeded'),
        event_context={'resource': resource, 'action_details': action_details},
    )


def _log_failed_action(resource, action, action_details):
    class_name = resource.__class__.__name__.lower()
    message = _get_action_message(action, action_details)
    log.event_logger.openstack_resource_action.warning(
        'Failed to execute "%s" operation for %s "%s"'
        % (message, class_name, resource.name),
        event_type=_get_action_event_type(action, 'failed'),
        event_context={'resource': resource, 'action_details': action_details},
    )


def _get_action_message(action, action_details):
    return action_details.pop('message', action)


def _get_action_event_type(action, event_state):
    return 'resource_%s_%s' % (action.replace(' ', '_').lower(), event_state)


def log_action(sender, instance, created=False, **kwargs):
    """ Log any resource action.

        Example of logged volume extend action:
        {
            'event_type': 'volume_extend_succeeded',
            'message': 'Successfully executed "Extend volume from 1024 MB to 2048 MB" operation for volume "pavel-test"',
            'action_details': {'old_size': 1024, 'new_size': 2048}
        }
    """
    resource = instance
    if created or not resource.tracker.has_changed('action'):
        return
    if resource.state == StateMixin.States.UPDATE_SCHEDULED:
        _log_scheduled_action(resource, resource.action, resource.action_details)
    if resource.state == StateMixin.States.OK:
        _log_succeeded_action(
            resource,
            resource.tracker.previous('action'),
            resource.tracker.previous('action_details'),
        )
    elif resource.state == StateMixin.States.ERRED:
        _log_failed_action(
            resource,
            resource.tracker.previous('action'),
            resource.tracker.previous('action_details'),
        )


def log_snapshot_schedule_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    snapshot_schedule = instance
    log.event_logger.openstack_snapshot_schedule.info(
        'Snapshot schedule "%s" has been created' % snapshot_schedule.name,
        event_type='resource_snapshot_schedule_created',
        event_context={
            'resource': snapshot_schedule.source_volume,
            'snapshot_schedule': snapshot_schedule,
        },
    )


def log_snapshot_schedule_action(sender, instance, created=False, **kwargs):
    snapshot_schedule = instance
    if created or not snapshot_schedule.tracker.has_changed('is_active'):
        return

    context = {
        'resource': snapshot_schedule.source_volume,
        'snapshot_schedule': snapshot_schedule,
    }
    if snapshot_schedule.is_active:
        log.event_logger.openstack_snapshot_schedule.info(
            'Snapshot schedule "%s" has been activated' % snapshot_schedule.name,
            event_type='resource_snapshot_schedule_activated',
            event_context=context,
        )
    else:
        if snapshot_schedule.error_message:
            message = (
                'Snapshot schedule "%s" has been deactivated because of error: %s'
                % (snapshot_schedule.name, snapshot_schedule.error_message)
            )
        else:
            message = (
                'Snapshot schedule "%s" has been deactivated' % snapshot_schedule.name
            )
        log.event_logger.openstack_snapshot_schedule.warning(
            message,
            event_type='resource_snapshot_schedule_deactivated',
            event_context=context,
        )


def log_snapshot_schedule_deletion(sender, instance, **kwargs):
    snapshot_schedule = instance
    log.event_logger.openstack_snapshot_schedule.info(
        'Snapshot schedule "%s" has been deleted' % snapshot_schedule.name,
        event_type='resource_snapshot_schedule_deleted',
        event_context={
            'resource': snapshot_schedule.source_volume,
            'snapshot_schedule': snapshot_schedule,
        },
    )


def log_backup_schedule_creation(sender, instance, created=False, **kwargs):
    if not created:
        return

    backup_schedule = instance
    log.event_logger.openstack_backup_schedule.info(
        'Backup schedule "%s" has been created' % backup_schedule.name,
        event_type='resource_backup_schedule_created',
        event_context={
            'resource': backup_schedule.instance,
            'backup_schedule': backup_schedule,
        },
    )


def log_backup_schedule_action(sender, instance, created=False, **kwargs):
    backup_schedule = instance
    if created or not backup_schedule.tracker.has_changed('is_active'):
        return

    context = {'resource': backup_schedule.instance, 'backup_schedule': backup_schedule}
    if backup_schedule.is_active:
        log.event_logger.openstack_backup_schedule.info(
            'Backup schedule "%s" has been activated' % backup_schedule.name,
            event_type='resource_backup_schedule_activated',
            event_context=context,
        )
    else:
        if backup_schedule.error_message:
            message = (
                'Backup schedule "%s" has been deactivated because of error: %s'
                % (backup_schedule.name, backup_schedule.error_message)
            )
        else:
            message = 'Backup schedule "%s" has been deactivated' % backup_schedule.name
        log.event_logger.openstack_backup_schedule.warning(
            message,
            event_type='resource_backup_schedule_deactivated',
            event_context=context,
        )


def log_backup_schedule_deletion(sender, instance, **kwargs):
    backup_schedule = instance
    log.event_logger.openstack_backup_schedule.info(
        'Backup schedule "%s" has been deleted' % backup_schedule.name,
        event_type='resource_backup_schedule_deleted',
        event_context={
            'resource': backup_schedule.instance,
            'backup_schedule': backup_schedule,
        },
    )


def update_service_settings_credentials(sender, instance, created=False, **kwargs):
    """
    Updates service settings credentials on tenant user_password or user_username change.
    It is possible to change a user password in tenant,
    as service settings copies tenant user password on creation it has to be update on change.
    """
    if created:
        return

    tenant = instance
    if tenant.tracker.has_changed('user_password') or tenant.tracker.has_changed(
        'user_username'
    ):
        service_settings = structure_models.ServiceSettings.objects.filter(
            scope=tenant
        ).first()
        if service_settings:
            service_settings.username = tenant.user_username
            service_settings.password = tenant.user_password
            service_settings.save()


class BaseSynchronizationHandler:
    """
    This class provides signal handlers for synchronization of OpenStack properties
    when parent OpenStack resource are created, updated or deleted.
    Security groups, floating IPs, networks and subnets are implemented as
    resources in openstack application. However they are implemented as service properties
    in the openstack_tenant application.
    """

    property_model = None
    resource_model = None
    fields = []

    def get_tenant(self, resource):
        return resource.tenant

    def get_service_settings(self, resource):
        try:
            return structure_models.ServiceSettings.objects.get(
                scope=self.get_tenant(resource),
                type=apps.OpenStackTenantConfig.service_name,
            )
        except (
            django_exceptions.ObjectDoesNotExist,
            django_exceptions.MultipleObjectsReturned,
        ):
            return

    def get_service_property(self, resource, settings):
        try:
            return self.property_model.objects.get(
                settings=settings, backend_id=resource.backend_id
            )
        except (
            django_exceptions.ObjectDoesNotExist,
            django_exceptions.MultipleObjectsReturned,
        ):
            return

    def map_resource_to_dict(self, resource):
        return {field: getattr(resource, field) for field in self.fields}

    def create_service_property(self, resource, settings):
        defaults = dict(name=resource.name, **self.map_resource_to_dict(resource))

        try:
            with transaction.atomic():
                return self.property_model.objects.get_or_create(
                    settings=settings, backend_id=resource.backend_id, defaults=defaults
                )
        except IntegrityError:
            logger.warning(
                'Could not create %s with backend ID %s '
                'and service settings %s due to concurrent update.',
                self.property_model,
                resource.backend_id,
                settings,
            )

    def update_service_property(self, resource, settings):
        service_property = self.get_service_property(resource, settings)
        if not service_property:
            return
        params = self.map_resource_to_dict(resource)
        for key, value in params.items():
            setattr(service_property, key, value)
        service_property.name = resource.name
        service_property.save()
        return service_property

    def create_handler(self, sender, instance, name, source, target, **kwargs):
        """
        Creates service property on resource transition from 'CREATING' state to 'OK'.
        """
        if source == StateMixin.States.CREATING and target == StateMixin.States.OK:
            settings = self.get_service_settings(instance)
            if settings and not self.get_service_property(instance, settings):
                self.create_service_property(instance, settings)

    def import_handler(self, sender, instance, created=False, **kwargs):
        """
        Creates service property on when resource is imported.
        """
        if created and instance.state == StateMixin.States.OK:
            settings = self.get_service_settings(instance)
            if settings and not self.get_service_property(instance, settings):
                self.create_service_property(instance, settings)

    def update_handler(self, sender, instance, name, source, target, **kwargs):
        """
        Updates service property on resource transition from 'UPDATING' state to 'OK'.
        """
        if source == StateMixin.States.UPDATING and target == StateMixin.States.OK:
            settings = self.get_service_settings(instance)
            if settings:
                self.update_service_property(instance, settings)

    def delete_handler(self, sender, instance, **kwargs):
        """
        Deletes service property on resource deletion
        """
        settings = self.get_service_settings(instance)
        if not settings:
            return
        service_property = self.get_service_property(instance, settings)
        if not service_property:
            return
        service_property.delete()


class FloatingIPHandler(BaseSynchronizationHandler):
    property_model = models.FloatingIP
    resource_model = openstack_models.FloatingIP
    fields = ('address', 'backend_network_id', 'runtime_state')


class SecurityGroupHandler(BaseSynchronizationHandler):
    property_model = models.SecurityGroup
    resource_model = openstack_models.SecurityGroup
    fields = ('description',)

    def map_rules(self, security_group, openstack_security_group):
        return [
            models.SecurityGroupRule(
                ethertype=rule.ethertype,
                direction=rule.direction,
                protocol=rule.protocol,
                from_port=rule.from_port,
                to_port=rule.to_port,
                cidr=rule.cidr,
                description=rule.description,
                backend_id=rule.backend_id,
                security_group=security_group,
            )
            for rule in openstack_security_group.rules.iterator()
        ]

    def create_service_property(self, resource, settings):
        service_property, _ = super(SecurityGroupHandler, self).create_service_property(
            resource, settings
        )
        if resource.rules.count() > 0:
            group_rules = self.map_rules(service_property, resource)
            service_property.rules.bulk_create(group_rules)
        return service_property

    def update_service_property(self, resource, settings):
        service_property = super(SecurityGroupHandler, self).update_service_property(
            resource, settings
        )
        if not service_property:
            return

        service_property.rules.all().delete()
        group_rules = self.map_rules(service_property, resource)
        service_property.rules.bulk_create(group_rules)
        return service_property


class NetworkHandler(BaseSynchronizationHandler):
    property_model = models.Network
    resource_model = openstack_models.Network
    fields = ('is_external', 'segmentation_id', 'type')


class SubNetHandler(BaseSynchronizationHandler):
    property_model = models.SubNet
    resource_model = openstack_models.SubNet
    fields = (
        'allocation_pools',
        'cidr',
        'dns_nameservers',
        'enable_dhcp',
        'ip_version',
        'is_connected',
    )

    def get_tenant(self, resource):
        return resource.network.tenant

    def map_resource_to_dict(self, resource):
        params = super(SubNetHandler, self).map_resource_to_dict(resource)
        params['network'] = models.Network.objects.get(
            backend_id=resource.network.backend_id
        )
        return params


resource_handlers = (
    FloatingIPHandler(),
    SecurityGroupHandler(),
    NetworkHandler(),
    SubNetHandler(),
)


def copy_flavor_exclude_regex_to_openstacktenant_service_settings(
    sender, instance, created=False, **kwargs
):
    if not created or instance.type != apps.OpenStackTenantConfig.service_name:
        return

    tenant = instance.scope
    if not isinstance(tenant, openstack_models.Tenant):
        return

    admin_settings = tenant.service_project_link.service.settings
    instance.options['flavor_exclude_regex'] = admin_settings.options.get(
        'flavor_exclude_regex', ''
    )
    instance.save(update_fields=['options'])


def copy_config_drive_to_openstacktenant_service_settings(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    if instance.type != openstack_apps.OpenStackConfig.service_name:
        return

    if not instance.tracker.has_changed('options'):
        return

    old_value = (instance.tracker.previous('options') or {}).get('config_drive', False)
    new_value = instance.options.get('config_drive', False)

    if old_value == new_value:
        return

    tenants = openstack_models.Tenant.objects.filter(
        service_project_link__service__settings=instance
    )
    ctype = ContentType.objects.get_for_model(openstack_models.Tenant)
    tenant_settings = structure_models.ServiceSettings.objects.filter(
        object_id__in=tenants.values_list('id'), content_type=ctype
    )
    for item in tenant_settings:
        item.options['config_drive'] = new_value
        item.save(update_fields=['options'])


def create_service_from_tenant(sender, instance, created=False, **kwargs):
    if not created:
        return

    if structure_models.ServiceSettings.objects.filter(
        scope=instance, type=apps.OpenStackTenantConfig.service_name,
    ).exists():
        return

    tenant = instance
    admin_settings = tenant.service_project_link.service.settings
    customer = tenant.service_project_link.project.customer
    service_settings = structure_models.ServiceSettings.objects.create(
        name=tenant.name,
        scope=tenant,
        customer=customer,
        type=apps.OpenStackTenantConfig.service_name,
        backend_url=admin_settings.backend_url,
        username=tenant.user_username,
        password=tenant.user_password,
        domain=admin_settings.domain,
        options={
            'availability_zone': tenant.availability_zone,
            'tenant_id': tenant.backend_id,
        },
    )

    if admin_settings.options.get('console_type'):
        service_settings.options['console_type'] = admin_settings.options.get(
            'console_type'
        )
        service_settings.save()

    if admin_settings.options.get('config_drive'):
        service_settings.options['config_drive'] = admin_settings.options[
            'config_drive'
        ]
        service_settings.save()

    service = models.OpenStackTenantService.objects.create(
        settings=service_settings, customer=customer,
    )

    models.OpenStackTenantServiceProjectLink.objects.create(
        service=service, project=tenant.service_project_link.project,
    )


def update_service_settings(sender, instance, created=False, **kwargs):
    tenant = instance

    if created or not (
        set(['external_network_id', 'name', 'backend_id'])
        & set(tenant.tracker.changed())
    ):
        return

    try:
        service_settings = structure_models.ServiceSettings.objects.get(
            scope=tenant, type=apps.OpenStackTenantConfig.service_name
        )
    except structure_models.ServiceSettings.DoesNotExist:
        return
    except structure_models.ServiceSettings.MultipleObjectsReturned:
        return
    else:
        service_settings.options['external_network_id'] = tenant.external_network_id
        service_settings.options['tenant_id'] = tenant.backend_id
        service_settings.name = tenant.name
        service_settings.save()


def sync_private_settings_quotas_with_tenant_quotas(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    quota = instance
    if not isinstance(quota.scope, openstack_models.Tenant):
        return

    tenant = quota.scope
    private_settings = structure_models.ServiceSettings.objects.filter(scope=tenant)

    Quota.objects.filter(scope__in=private_settings, name=quota.name).update(
        limit=quota.limit, usage=quota.usage
    )


def propagate_volume_type_quotas_from_tenant_to_private_service_settings(
    sender, instance, created=False, **kwargs
):
    quota = instance

    if not created:
        return

    if not quota.name.startswith('gigabytes_'):
        return

    if not isinstance(quota.scope, openstack_models.Tenant):
        return

    tenant = quota.scope
    private_settings = structure_models.ServiceSettings.objects.filter(scope=tenant)
    for service_settings in private_settings:
        Quota.objects.update_or_create(
            content_type=ContentType.objects.get_for_model(service_settings),
            object_id=service_settings.id,
            name=quota.name,
            defaults=dict(limit=quota.limit, usage=quota.usage,),
        )


def delete_volume_type_quotas_from_private_service_settings(sender, instance, **kwargs):
    quota = instance

    if not quota.name.startswith('gigabytes_'):
        return

    if not isinstance(quota.scope, openstack_models.Tenant):
        return

    tenant = quota.scope
    private_settings = structure_models.ServiceSettings.objects.filter(scope=tenant)
    Quota.objects.filter(scope__in=private_settings, name=quota.name).delete()


def update_remote_group_when_rule_is_updated(sender, instance, **kwargs):
    if not instance.tracker.has_changed('remote_group_id'):
        return

    try:
        service_settings = structure_models.ServiceSettings.objects.get(
            scope=instance.security_group.tenant,
            type=apps.OpenStackTenantConfig.service_name,
        )
    except (
        django_exceptions.ObjectDoesNotExist,
        django_exceptions.MultipleObjectsReturned,
    ):
        return

    try:
        security_group = models.SecurityGroup.objects.get(
            settings=service_settings, backend_id=instance.security_group.backend_id
        )
    except django_exceptions.ObjectDoesNotExist:
        return

    remote_group_id = None
    if instance.remote_group:
        try:
            remote_group_id = models.SecurityGroup.objects.get(
                settings=service_settings, backend_id=instance.remote_group.backend_id
            ).id
        except django_exceptions.ObjectDoesNotExist:
            pass

    models.SecurityGroupRule.objects.filter(
        security_group=security_group, backend_id=instance.backend_id
    ).update(remote_group_id=remote_group_id)
