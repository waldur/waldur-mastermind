from __future__ import unicode_literals

import factory
from rest_framework.reverse import reverse
from waldur_ansible.jupyter_hub_management import models
from waldur_ansible.python_management.tests import factories as python_management_factories
from waldur_openstack.openstack_tenant.tests import factories as openstack_factories

from waldur_core.core import utils as core_utils
from waldur_core.structure.tests import factories as structure_factories


class JupyterHubOAuthConfigFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.JupyterHubOAuthConfig

    type = factory.Sequence(lambda n: n)
    oauth_callback_url = factory.Sequence(lambda n: 'oauth_callback_url%s' % n)
    client_id = factory.Sequence(lambda n: 'client_id%s' % n)
    client_secret = factory.Sequence(lambda n: 'client_secret%s' % n)
    tenant_id = factory.Sequence(lambda n: 'tenant_id%s' % n)
    gitlab_host = factory.Sequence(lambda n: 'gitlab_host%s' % n)


class JupyterHubManagementFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.JupyterHubManagement

    user = factory.SubFactory(structure_factories.UserFactory)
    instance = factory.SubFactory(openstack_factories.InstanceFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    python_management = factory.SubFactory(python_management_factories.PythonManagementFactory)
    jupyter_hub_oauth_config = factory.SubFactory(JupyterHubOAuthConfigFactory)
    session_time_to_live_hours = factory.Sequence(lambda n: n)

    @classmethod
    def get_url(cls, jupyter_hub_management=None, action=None):
        if jupyter_hub_management is None:
            jupyter_hub_management = JupyterHubManagementFactory()

        url = 'http://testserver' + reverse(core_utils.get_detail_view_name(models.JupyterHubManagement), kwargs={'uuid': jupyter_hub_management.uuid})
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse(core_utils.get_list_view_name(models.JupyterHubManagement))


class JupyterHubManagementSyncConfigurationRequestFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.JupyterHubManagementSyncConfigurationRequest

    jupyter_hub_management = factory.SubFactory(JupyterHubManagementFactory)
    output = factory.Sequence(lambda n: n)


class JupyterHubManagementMakeVirtualEnvironmentGlobalRequestFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.JupyterHubManagementMakeVirtualEnvironmentGlobalRequest

    jupyter_hub_management = factory.SubFactory(JupyterHubManagementFactory)
    update_configuration_request = factory.SubFactory(JupyterHubManagementSyncConfigurationRequestFactory)
    output = factory.Sequence(lambda n: n)
    virtual_env_name = factory.Sequence(lambda n: n)


class JupyterHubManagementDeleteRequestFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.JupyterHubManagementDeleteRequest

    jupyter_hub_management = factory.SubFactory(JupyterHubManagementFactory)
    output = factory.Sequence(lambda n: n)


class JupyterHubManagementMakeVirtualEnvironmentLocalRequestFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.JupyterHubManagementMakeVirtualEnvironmentLocalRequest

    jupyter_hub_management = factory.SubFactory(JupyterHubManagementFactory)
    output = factory.Sequence(lambda n: n)
    virtual_env_name = factory.Sequence(lambda n: n)


class JupyterHubUserFactory(factory.DjangoModelFactory):
    class Meta(object):
        model = models.JupyterHubUser

    username = factory.Sequence(lambda n: 'username%s' % n)
    password = factory.Sequence(lambda n: 'password%s' % n)
    whitelisted = factory.Sequence(lambda n: n)
    admin = factory.Sequence(lambda n: n)
    jupyter_hub_management = factory.SubFactory(JupyterHubManagementFactory)
