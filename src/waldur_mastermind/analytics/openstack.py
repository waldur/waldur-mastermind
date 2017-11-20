from waldur_openstack.openstack.models import Tenant
from waldur_openstack.openstack_tenant.models import Instance


def get_tenants():
    if not Tenant.objects.exists():
        return []
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
    states = count_instances_by_state(Instance.objects.all())

    points = []
    for state, count in states.items():
        points.append({
            'measurement': 'openstack_instance_runtime_state',
            'tags': {
                'state': state,
            },
            'fields': {
                'count': count,
            }
        })
    return points


def count_instances_by_state(instances):
    erred = 0
    online = 0
    offline = 0
    provisioning = 0

    for instance in instances.values('state', 'runtime_state'):
        if instance['state'] == Instance.States.ERRED:
            erred += 1
        elif instance['state'] != Instance.States.OK:
            provisioning += 1
        elif instance['runtime_state'] == Instance.get_online_state():
            online += 1
        elif instance['runtime_state'] == Instance.get_offline_state():
            offline += 1

    return {
        'erred': erred,
        'online': online,
        'offline': offline,
        'provisioning': provisioning,
        'total': erred + online + offline + provisioning
    }
