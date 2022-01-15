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
        }

    @staticmethod
    def get_cleanup_executor():
        from .executors import OpenStackCleanupExecutor

        return OpenStackCleanupExecutor
