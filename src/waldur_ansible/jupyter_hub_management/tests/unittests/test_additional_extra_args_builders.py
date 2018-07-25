from django.test import TestCase
from waldur_ansible.jupyter_hub_management import models
from waldur_ansible.jupyter_hub_management.backend import additional_extra_args_builders
from waldur_ansible.jupyter_hub_management.tests import factories, fixtures


class JupyterHubManagementExtraArgsBuildersTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.JupyterHubManagementOAuthFixture()
        self.jupyter_hub_management = self.fixture.jupyter_hub_management
        self.sync_request = factories.JupyterHubManagementSyncConfigurationRequestFactory(jupyter_hub_management=self.jupyter_hub_management)

    def test_build_sync_config_extra_args_no_oauth(self):
        self.jupyter_hub_management.jupyter_hub_oauth_config = None
        self.jupyter_hub_management.jupyter_hub_users = [self.fixture.jupyter_hub_admin_user, self.fixture.jupyter_hub_whitelisted_user]

        extra_vars = additional_extra_args_builders.build_sync_config_extra_args(self.sync_request)

        self.assertEquals(extra_vars['session_timeout_seconds'], self.jupyter_hub_management.session_time_to_live_hours * 3600)
        self.assertIsNone(extra_vars.get('oauth_config', None))
        self.assertIn(self.fixture.jupyter_hub_admin_user.username, map(lambda u: u['username'], extra_vars['all_jupyterhub_users']))
        self.assertIn(self.fixture.jupyter_hub_admin_user.password, map(lambda u: u['password'], extra_vars['all_jupyterhub_users']))
        self.assertIn(self.fixture.jupyter_hub_whitelisted_user.username, map(lambda u: u['username'], extra_vars['all_jupyterhub_users']))

    def test_build_sync_config_extra_args_with_oauth(self):
        self.jupyter_hub_management.jupyter_hub_oauth_config = self.fixture.jupyter_hub_oauth_config
        self.jupyter_hub_management.jupyter_hub_users = [self.fixture.jupyter_hub_admin_user, self.fixture.jupyter_hub_whitelisted_user]

        extra_vars = additional_extra_args_builders.build_sync_config_extra_args(self.sync_request)

        self.assertEquals(extra_vars['session_timeout_seconds'], self.jupyter_hub_management.session_time_to_live_hours * 3600)
        self.assertIn(self.fixture.jupyter_hub_admin_user.username, map(lambda u: u['username'], extra_vars['jupyterhub_admin_users']))
        self.assertIn(self.fixture.jupyter_hub_whitelisted_user.username, map(lambda u: u['username'], extra_vars['jupyterhub_whitelisted_users']))
        self.assertNotIn(self.fixture.jupyter_hub_whitelisted_user.username, map(lambda u: u['username'], extra_vars['jupyterhub_admin_users']))
        self.assertNotIn(self.fixture.jupyter_hub_admin_user.username, map(lambda u: u['username'], extra_vars['jupyterhub_whitelisted_users']))

    def test_build_sync_config_extra_args_with_azure(self):
        self.fixture.jupyter_hub_oauth_config.type = models.JupyterHubOAuthType.AZURE
        self.jupyter_hub_management.jupyter_hub_oauth_config = self.fixture.jupyter_hub_oauth_config

        extra_vars = additional_extra_args_builders.build_sync_config_extra_args(self.sync_request)

        self.assertEquals(extra_vars['oauth_config']['tenant_id'], self.fixture.jupyter_hub_oauth_config.tenant_id)
