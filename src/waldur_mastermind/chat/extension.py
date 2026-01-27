from waldur_core.core import WaldurExtension


class MarketplaceChatExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.chat"

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
            "waldur-chat-reset-daily-token-usage": {
                "task": "waldur_mastermind.chat.reset_daily_token_usage",
                "schedule": crontab(minute=0, hour=0),
                "args": (),
            },
            "waldur-chat-reset-weekly-token-usage": {
                "task": "waldur_mastermind.chat.reset_weekly_token_usage",
                "schedule": crontab(minute=0, hour=0, day_of_week=1),
                "args": (),
            },
            "waldur-chat-reset-monthly-token-usage": {
                "task": "waldur_mastermind.chat.reset_monthly_token_usage",
                "schedule": crontab(minute=0, hour=0, day_of_month=1),
                "args": (),
            },
        }
