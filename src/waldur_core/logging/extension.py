from waldur_core.core import WaldurExtension


class LoggingExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_core.logging"
