from waldur_ansible.jupyter_hub_management import models


def build_virtual_env_extra_args(jupyter_hub_virtual_env_request):
    return dict(
        virtual_env_name=jupyter_hub_virtual_env_request.virtual_env_name
    )


def build_delete_jupyter_hub_extra_args(delete_jupyter_hub_request):
    return dict(
        all_jupyterhub_users=map(lambda u: u.username, delete_jupyter_hub_request.jupyter_hub_management.jupyter_hub_users.all()),
        first_admin_username=delete_jupyter_hub_request.jupyter_hub_management.get_admin_users()[0].username
    )


def build_sync_config_extra_args(sync_config_request):
    def user_password_pair_builder(user):
        return dict(username=user.username, password=user.password)

    persisted_oauth_config = sync_config_request.jupyter_hub_management.jupyter_hub_oauth_config

    all_jupyter_hub_users = sync_config_request.jupyter_hub_management.jupyter_hub_users.all()
    jupyterhub_whitelisted_users = sync_config_request.jupyter_hub_management.get_whitelisted_users()
    extra_vars = dict(
        session_timeout_seconds=sync_config_request.jupyter_hub_management.session_time_to_live_hours * 3600,
        all_jupyterhub_users=map(user_password_pair_builder, all_jupyter_hub_users),
        jupyterhub_admin_users=map(user_password_pair_builder, sync_config_request.jupyter_hub_management.get_admin_users()),
        jupyterhub_whitelisted_users=map(user_password_pair_builder, jupyterhub_whitelisted_users if persisted_oauth_config else all_jupyter_hub_users),
    )

    if persisted_oauth_config:
        oauth_config = dict(
            type=persisted_oauth_config.type,
            oauth_callback_url=persisted_oauth_config.oauth_callback_url,
            client_id=persisted_oauth_config.client_id,
            client_secret=persisted_oauth_config.client_secret,
            gitlab_host=persisted_oauth_config.gitlab_host,
            tenant_id=persisted_oauth_config.tenant_id if persisted_oauth_config.type == models.JupyterHubOAuthType.AZURE else None)
        extra_vars['oauth_config'] = oauth_config

    return extra_vars
