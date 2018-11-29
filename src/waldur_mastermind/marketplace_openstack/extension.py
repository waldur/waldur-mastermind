from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class MarketplaceOpenStackExtension(WaldurExtension):

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_openstack'

    @staticmethod
    def is_assembly():
        return True
