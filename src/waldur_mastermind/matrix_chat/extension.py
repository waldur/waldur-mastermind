from celery.schedules import crontab

from waldur_core.core import WaldurExtension


class MatrixChatExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.matrix_chat"

    @staticmethod
    def is_assembly():
        return True

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
        return {
            "waldur-matrix-chat-periodic-history-export": {
                "task": "waldur_mastermind.matrix_chat.periodic_history_export",
                "schedule": crontab(minute=0, hour=2),
                "args": (),
            },
            "waldur-matrix-chat-cleanup-appservice-transactions": {
                "task": "waldur_mastermind.matrix_chat.cleanup_old_appservice_transactions",
                "schedule": crontab(minute=15, hour=3),
                "args": (),
            },
        }
