from django.db.models import Count

from nodeconductor_openstack.openstack.models import Tenant
from nodeconductor_openstack.openstack_tenant.models import Instance


def get_tenants():
    points = []
    tenants = Tenant.objects.all()
    quota_names = Tenant.get_quotas_names()
    quotas = Tenant.get_sum_of_quotas_as_dict(tenants, quota_names=quota_names)

    for quota_name in quota_names:
        points.append({
            'measurement': 'openstack_%s' % quota_name,
            'fields': {
                'limit': quotas[quota_name],
                'usage': quotas['%s_usage' % quota_name],
            }
        })
    return points


def get_instances():
    points = []
    for item in Instance.objects.values('runtime_state').annotate(count=Count('runtime_state')):
        points.append({
            'measurement': 'openstack_instance_runtime_state',
            'tags': {
                'runtime_state': item['runtime_state'],
            },
            'fields': {
                'count': item['count'],
            }
        })
    return points
