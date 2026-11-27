from django.apps import AppConfig


def _is_hpc_configured() -> bool:
    from waldur_firecrest import workspace

    return workspace.is_configured()


def _get_hpc_resources_for_user(user):
    from waldur_firecrest import workspace

    return workspace.resources_for_user(user)


class FirecrestConfig(AppConfig):
    name = "waldur_firecrest"
    verbose_name = "FirecREST"

    def ready(self):
        from waldur_firecrest import chat_tools  # noqa: F401 registers HPC chat tools
        from waldur_mastermind.chat import hooks as chat_hooks

        chat_hooks.register_hpc_workspace_provider(
            chat_hooks.HpcWorkspaceProvider(
                is_configured=_is_hpc_configured,
                resources_for_user=_get_hpc_resources_for_user,
            )
        )
