from waldur_core.logging.loggers import EventLogger, event_logger


class TenantQuotaLogger(EventLogger):
    quota = 'quotas.Quota'
    tenant = 'openstack.Tenant'
    limit = float
    old_limit = float

    class Meta:
        event_types = ('openstack_tenant_quota_limit_updated',)
        event_groups = {
            'resources': event_types,
        }


event_logger.register('openstack_tenant_quota', TenantQuotaLogger)
