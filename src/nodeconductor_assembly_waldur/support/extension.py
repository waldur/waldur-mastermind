from __future__ import unicode_literals

from nodeconductor.core import NodeConductorExtension


class SupportExtension(NodeConductorExtension):

    class Settings(object):
        WALDUR_SUPPORT_CREDENTIALS = {
            'password': 'PASSWORD',
            'project': 'PROJECT',
            'server': 'http://example.com/',
            'username': 'USERNAME'
        }

    @staticmethod
    def django_app():
        return 'nodeconductor_assembly_waldur.support'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def is_assembly():
        return True
