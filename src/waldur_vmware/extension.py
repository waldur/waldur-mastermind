from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class VMwareExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_vmware'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in
