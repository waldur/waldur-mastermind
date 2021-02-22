from waldur_core.core import WaldurExtension


class MarketplaceSlurmExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_slurm'

    @staticmethod
    def is_assembly():
        return True
