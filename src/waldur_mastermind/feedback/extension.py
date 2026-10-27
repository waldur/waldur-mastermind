from django.utils import timezone

from waldur_core.core import WaldurExtension


class FeedbackExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.feedback"

    @staticmethod
    def is_assembly():
        return True

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def celery_tasks():
        return {
            "expire-feedback-requests": {
                "task": "waldur_mastermind.feedback.expire_requests",
                "schedule": timezone.timedelta(hours=1),
                "args": (),
            },
        }
