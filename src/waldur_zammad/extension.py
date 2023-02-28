from waldur_core.core import WaldurExtension


class ZammadExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return 'waldur_zammad'
