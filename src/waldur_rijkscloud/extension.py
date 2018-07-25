from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class RijkscloudExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_rijkscloud'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in
