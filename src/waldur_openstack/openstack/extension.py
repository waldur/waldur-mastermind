from waldur_core.core import WaldurExtension


class OpenStackExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_openstack.openstack'

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            'openstack-tenant-pull-quotas': {
                'task': 'openstack.TenantPullQuotas',
                'schedule': timedelta(hours=12),
                'args': (),
            },
            'openstack_mark_as_erred_old_tenants_in_deleting_state': {
                'task': 'openstack.mark_as_erred_old_tenants_in_deleting_state',
                'schedule': timedelta(hours=24),
                'args': (),
            },
        }

    @staticmethod
    def get_cleanup_executor():
        from .executors import OpenStackCleanupExecutor

        return OpenStackCleanupExecutor
