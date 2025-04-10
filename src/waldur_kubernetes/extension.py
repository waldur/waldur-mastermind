from waldur_core.core import WaldurExtension


class KubernetesExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_kubernetes"
