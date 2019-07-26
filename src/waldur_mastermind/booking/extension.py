from __future__ import unicode_literals

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
