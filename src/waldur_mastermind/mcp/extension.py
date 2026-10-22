from waldur_core.core import WaldurExtension


class MCPExtension(WaldurExtension):
    @staticmethod
    def django_app():
        return "waldur_mastermind.mcp"
