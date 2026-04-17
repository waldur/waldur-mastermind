from waldur_core.core import WaldurExtension


class PolicyExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.policy"

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        from celery.schedules import crontab

        return {
            "check-polices": {
                "task": "waldur_mastermind.policy.check_polices",
                "schedule": crontab(hour=2, minute=0),
                "args": (),
            },
            "cleanup-slurm-evaluation-logs": {
                "task": "waldur_mastermind.policy.cleanup_slurm_evaluation_logs",
                "schedule": crontab(hour=3, minute=0),
                "args": (),
            },
            "reset-slurm-policy-periods": {
                "task": "waldur_mastermind.policy.reset_slurm_policies_on_period_boundary",
                "schedule": crontab(hour=1, minute=0),
                "args": (),
            },
            "sync-slurm-periodic-settings": {
                "task": "waldur_mastermind.policy.sync_slurm_periodic_settings",
                "schedule": crontab(minute="*/10"),
                "args": (),
            },
        }
