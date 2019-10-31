from waldur_core.core import WaldurExtension


class BookingExtension(WaldurExtension):
    class Settings:
        pass

    @staticmethod
    def django_app():
        return 'waldur_mastermind.booking'

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
            'waldur-mastermind-send-notifications-about-upcoming-bookings': {
                'task': 'waldur_mastermind.booking.send_notifications_about_upcoming_bookings',
                'schedule': crontab(minute=0, hour=9),
                'args': (),
            }
        }
