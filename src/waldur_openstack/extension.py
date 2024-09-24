from waldur_core.core import WaldurExtension


class OpenStackExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_openstack"

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def django_urls():
        from .urls import urlpatterns

        return urlpatterns

    @staticmethod
    def celery_tasks():
        from datetime import timedelta

        return {
            "openstack-tenant-pull-quotas": {
                "task": "openstack.TenantPullQuotas",
                "schedule": timedelta(hours=12),
                "args": (),
            },
            "openstack_mark_as_erred_old_tenants_in_deleting_state": {
                "task": "openstack.mark_as_erred_old_tenants_in_deleting_state",
                "schedule": timedelta(hours=24),
                "args": (),
            },
            "openstack-schedule-backups": {
                "task": "openstack.ScheduleBackups",
                "schedule": timedelta(minutes=10),
                "args": (),
            },
            "openstack-delete-expired-backups": {
                "task": "openstack.DeleteExpiredBackups",
                "schedule": timedelta(minutes=10),
                "args": (),
            },
            "openstack-schedule-snapshots": {
                "task": "openstack.ScheduleSnapshots",
                "schedule": timedelta(minutes=10),
                "args": (),
            },
            "openstack-delete-expired-snapshots": {
                "task": "openstack.DeleteExpiredSnapshots",
                "schedule": timedelta(minutes=10),
                "args": (),
            },
        }

    @staticmethod
    def get_cleanup_executor():
        from .executors import OpenStackCleanupExecutor

        return OpenStackCleanupExecutor
