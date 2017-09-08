from __future__ import unicode_literals

from nodeconductor.core import NodeConductorExtension


class BillingExtension(NodeConductorExtension):
    @staticmethod
    def django_app():
        return 'nodeconductor_assembly_waldur.billing'

    @staticmethod
    def rest_urls():
        from .urls import register_in
        return register_in

    @staticmethod
    def is_assembly():
        return True
