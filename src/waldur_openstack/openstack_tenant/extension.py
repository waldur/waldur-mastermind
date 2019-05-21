from waldur_core.core import WaldurExtension


class OpenStackTenantExtension(WaldurExtension):

    class Settings:
        # wiki: https://opennode.atlassian.net/wiki/display/WD/OpenStack+plugin+configuration
        WALDUR_OPENSTACK_TENANT = {
            'MAX_CONCURRENT_PROVISION': {
                'OpenStackTenant.Instance': 4,
                'OpenStackTenant.Volume': 4,
                'OpenStackTenant.Snapshot': 4,
            },
            'ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS': False,
        }

    @staticmethod
    def get_public_settings():
        return [
            'ALLOW_CUSTOMER_USERS_OPENSTACK_CONSOLE_ACCESS',
        ]

    @staticmethod
    def django_app():
        return 'waldur_openstack.openstack_tenant'

    @staticmethod
    def django_urls():
        from .urls import urlpatterns
        return urlpatterns

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def celery_tasks():
        from datetime import timedelta
        return {
            'openstacktenant-schedule-backups': {
                'task': 'openstack_tenant.ScheduleBackups',
                'schedule': timedelta(minutes=10),
                'args': (),
            },
            'openstacktenant-delete-expired-backups': {
                'task': 'openstack_tenant.DeleteExpiredBackups',
                'schedule': timedelta(minutes=10),
                'args': (),
            },
            'openstacktenant-schedule-snapshots': {
                'task': 'openstack_tenant.ScheduleSnapshots',
                'schedule': timedelta(minutes=10),
                'args': (),
            },
            'openstacktenant-delete-expired-snapshots': {
                'task': 'openstack_tenant.DeleteExpiredSnapshots',
                'schedule': timedelta(minutes=10),
                'args': (),
            },
        }

    @staticmethod
    def get_cleanup_executor():
        from .executors import OpenStackTenantCleanupExecutor
        return OpenStackTenantCleanupExecutor
