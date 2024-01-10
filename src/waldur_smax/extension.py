from waldur_core.core import WaldurExtension


class SmaxExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_smax"
