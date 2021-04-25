from waldur_core.core import WaldurExtension


class HPCExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_hpc'
