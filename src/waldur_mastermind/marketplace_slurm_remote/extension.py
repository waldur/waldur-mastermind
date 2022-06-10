from waldur_core.core import WaldurExtension


class MarketplaceSlurmRemoteExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_mastermind.marketplace_slurm_remote'

    @staticmethod
    def rest_urls():
        from .urls import register_in

        return register_in

    @staticmethod
    def is_assembly():
        return True
