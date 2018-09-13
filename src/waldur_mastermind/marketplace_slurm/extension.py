from __future__ import unicode_literals

from waldur_core.core import WaldurExtension


class MarketplaceSlurmExtension(WaldurExtension):

    class Settings:
        WALDUR_MARKETPLACE_SLURM = {}

    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_slurm'

    @staticmethod
    def is_assembly():
        return True
