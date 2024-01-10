from datetime import timedelta

from waldur_core.core import WaldurExtension


class PIDExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_pid"

    @staticmethod
    def celery_tasks():
        return {
            "waldur-pid-update-all-referrables": {
                "task": "waldur_pid.update_all_referrables",
                "schedule": timedelta(days=1),
                "args": (),
            },
            "waldur-update-all-pid": {
                "task": "waldur_pid.update_all_pid",
                "schedule": timedelta(days=1),
                "args": (),
            },
        }
