from waldur_core.core import WaldurExtension


class OpenPortalExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_openportal"

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
            # This task re-tries to add or remove local users in case
            # user management failed when called directly
            "waldur-openportal-sync-local-users": {
                "task": "waldur_openportal.sync_local_users",
                "schedule": timedelta(minutes=19),
                "args": (),
            },
            # This task re-tries to add or remove remote users in case
            # user management failed when called directly
            "waldur-openportal-sync-remote-users": {
                "task": "waldur_openportal.sync_remote_users",
                "schedule": timedelta(minutes=23),
                "args": (),
            },
            # This task synchronises all usage from the OpenPortal resources
            # back to the portal - this is the "near-real time" consumption
            # data
            "waldur-openportal-sync-usage": {
                "task": "waldur_openportal.sync_usage",
                "schedule": timedelta(minutes=7),
                "args": (),
            },
            # This task updates the resource limits for all allocations
            "waldur-openportal-sync-allocation-limits": {
                "task": "waldur_openportal.sync_allocation_limits",
                "schedule": timedelta(minutes=17),
                "args": (),
            },
            # This task checks if we need to email project members with
            # their fortnightly project consumption statistics email
            "waldur-openportal-send-notifications": {
                "task": "waldur_openportal.send_notifications",
                "schedule": timedelta(minutes=47),
                "args": (),
            },
            # This task re-tries to add or update remote projects
            # in case project management failed when called directly.
            # This is important for, e.g. when a project needs approving,
            # and it can be a while until approval is granted
            "waldur-openportal-sync-remote": {
                "task": "waldur_openportal.sync_remote",
                "schedule": timedelta(minutes=29),
                "args": (),
            },
            # This task synchronises all usage from remote OpenPortal resources
            # back to this remote portal. This is the "near-real time"
            # consumption data visible in the remote portal
            "waldur-openportal-sync-remote-usage": {
                "task": "waldur_openportal.sync_remote_usage",
                "schedule": timedelta(minutes=9),
                "args": (),
            },
            # This task synchronises all offering agents
            "waldur-openportal-sync-offering-agents": {
                "task": "waldur_openportal.sync_offering_agents",
                "schedule": timedelta(minutes=19),
                "args": (),
            },
            # This task fetches storage snapshots for all active allocations and
            # accumulates them into monthly CachedProjectStorageReport records.
            # 8 hours is frequent enough for at least one snapshot per day without
            # overloading the filesystems.
            "waldur-openportal-sync-storage": {
                "task": "waldur_openportal.sync_storage",
                "schedule": timedelta(hours=8),
                "args": (),
            },
            # This task fetches accumulated storage reports from remote portals
            # for all active RemoteAllocations and stores them locally.
            "waldur-openportal-sync-remote-storage": {
                "task": "waldur_openportal.sync_remote_storage",
                "schedule": timedelta(hours=8),
                "args": (),
            },
            # This task cleans up stale OpenPortal jobs from the database
            "waldur_openportal.clean_stale_jobs": {
                "task": "waldur_openportal.clean_stale_jobs",
                "schedule": timedelta(minutes=60 * 24),
                "args": (),
            },
            # This task fixes the project credit allocation, to prevent
            # drift from what is set by the ManagedProject
            "waldur_openportal.fix_total_allocation": {
                "task": "waldur_openportal.fix_total_allocation",
                "schedule": timedelta(minutes=60 * 24),
                "args": (),
            },
        }

    @staticmethod
    def get_cleanup_executor():
        from waldur_openportal.executors import OpenPortalCleanupExecutor

        return OpenPortalCleanupExecutor
