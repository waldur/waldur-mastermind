from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class RancherExtension(WaldurExtension):

    class Settings:
        WALDUR_RANCHER = {}

    @staticmethod
    def django_app():
        return 'waldur_rancher'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in
