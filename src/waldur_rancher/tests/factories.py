import factory
from django.urls import reverse

from waldur_core.structure.tests import factories as structure_factories
from waldur_rancher import enums, models


class RancherServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    type = "Rancher"
    backend_url = "https://example.com"
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class ClusterFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Cluster

    name = factory.Sequence(lambda n: "cluster-%s" % n)
    backend_id = factory.Sequence(lambda n: "cluster-%s" % n)
    settings = factory.SubFactory(RancherServiceSettingsFactory)
    service_settings = factory.SubFactory(RancherServiceSettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)

    @classmethod
    def get_url(cls, cluster=None, action=None):
        cluster = cluster or ClusterFactory()
        url = "http://testserver" + reverse(
            "rancher-cluster-detail", kwargs={"uuid": cluster.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls, action=None):
        url = "http://testserver" + reverse("rancher-cluster-list")
        return url if action is None else url + action + "/"


class NodeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Node

    name = factory.Sequence(lambda n: "node-%s" % n)
    cluster = factory.SubFactory(ClusterFactory)
    backend_id = factory.Sequence(lambda n: "node-%s" % n)

    @classmethod
    def get_url(cls, node=None, action=None):
        node = node or NodeFactory()
        url = "http://testserver" + reverse(
            "rancher-node-detail", kwargs={"uuid": node.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("rancher-node-list")


class RancherUserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.RancherUser

    user = factory.SubFactory(structure_factories.UserFactory)
    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: "rancher-user-%s" % n)
    is_active = True

    @classmethod
    def get_url(cls, user=None, action=None):
        user = user or RancherUserFactory()
        url = "http://testserver" + reverse(
            "rancher-user-detail", kwargs={"uuid": user.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("rancher-user-list")


class RancherUserClusterLinkFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.RancherUserClusterLink

    user = factory.SubFactory(RancherUserFactory)
    cluster = factory.SubFactory(ClusterFactory)
    role = models.ClusterRole.CLUSTER_OWNER
    backend_id = factory.Sequence(lambda n: "rancher-user-cluster-link-%s" % n)


class CatalogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Catalog

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: "catalog-%s" % n)

    @classmethod
    def get_url(cls, catalog=None, action=None):
        catalog = catalog or CatalogFactory()
        url = "http://testserver" + reverse(
            "rancher-catalog-detail", kwargs={"uuid": catalog.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("rancher-catalog-list")


class TemplateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Template

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: "template-%s" % n)
    versions = []

    @classmethod
    def get_url(cls, template=None, action=None):
        template = template or TemplateFactory()
        url = "http://testserver" + reverse(
            "rancher-template-detail", kwargs={"uuid": template.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("rancher-template-list")


class ProjectFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Project

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: "project-%s" % n)

    @classmethod
    def get_url(cls, project=None, action=None):
        project = project or ProjectFactory()
        url = "http://testserver" + reverse(
            "rancher-project-detail", kwargs={"uuid": project.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("rancher-project-list")


class NamespaceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.Namespace

    settings = factory.SubFactory(RancherServiceSettingsFactory)
    backend_id = factory.Sequence(lambda n: "namespace-%s" % n)

    @classmethod
    def get_url(cls, namespace=None, action=None):
        namespace = namespace or NamespaceFactory()
        url = "http://testserver" + reverse(
            "rancher-namespace-detail", kwargs={"uuid": namespace.uuid.hex}
        )
        return url if action is None else url + action + "/"

    @classmethod
    def get_list_url(cls):
        return "http://testserver" + reverse("rancher-namespace-list")


class RancherUserProjectLinkFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = models.RancherUserProjectLink

    user = factory.SubFactory(RancherUserFactory)
    project = factory.SubFactory(ProjectFactory)
    role = enums.ProjectRoleId.project_owner
    backend_id = factory.Sequence(lambda n: "rancher-user-project-link-%s" % n)
