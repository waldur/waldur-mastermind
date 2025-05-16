import factory
from django.urls import reverse

from waldur_core.core.tests.types import BaseMetaFactory
from waldur_core.structure.tests import factories as structure_factories
from waldur_rancher import enums, models


class RancherServiceSettingsFactory(structure_factories.ServiceSettingsFactory):
    type = "Rancher"
    backend_url = "https://example.com"
    customer = factory.SubFactory(structure_factories.CustomerFactory)


class ClusterFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Cluster]
):
    class Meta:
        model = models.Cluster

    name = factory.Sequence(lambda n: "cluster-%s" % n)
    backend_id = factory.Sequence(lambda n: "cluster-%s" % n)
    settings = factory.SubFactory(RancherServiceSettingsFactory)
    service_settings = factory.SubFactory(RancherServiceSettingsFactory)
    project = factory.SubFactory(structure_factories.ProjectFactory)
    vm_project = factory.SubFactory(structure_factories.ProjectFactory)

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


class NodeFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Node]
):
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


class RoleTemplateFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.RoleTemplate]
):
    class Meta:
        model = models.RoleTemplate

    scope_type = enums.RoleScopeType.CLUSTER
    settings = factory.SubFactory(RancherServiceSettingsFactory)
    name = factory.Sequence(lambda n: f"test-role-template-{n}")
    display_name = factory.Sequence(lambda n: f"test-role-template-display-{n}")


class RancherUserFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.RancherUser]
):
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


class RancherUserClusterLinkFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.RancherUserClusterLink],
):
    class Meta:
        model = models.RancherUserClusterLink

    user = factory.SubFactory(RancherUserFactory)
    cluster = factory.SubFactory(ClusterFactory)
    role = factory.SubFactory(RoleTemplateFactory)
    backend_id = factory.Sequence(lambda n: "rancher-user-cluster-link-%s" % n)


class CatalogFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Catalog]
):
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


class TemplateFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Template]
):
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


class ProjectFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Project]
):
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


class NamespaceFactory(
    factory.django.DjangoModelFactory, metaclass=BaseMetaFactory[models.Namespace]
):
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


class RancherUserProjectLinkFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.RancherUserProjectLink],
):
    class Meta:
        model = models.RancherUserProjectLink

    user = factory.SubFactory(RancherUserFactory)
    project = factory.SubFactory(ProjectFactory)
    role = factory.SubFactory(
        RoleTemplateFactory,
        defaults={
            "scope_type": enums.RoleScopeType.PROJECT,
            "name": "project-owner",
            "display_name": "Project Owner",
        },
    )
    backend_id = factory.Sequence(lambda n: "rancher-user-project-link-%s" % n)


class ClusterSecurityGroupFactory(
    factory.django.DjangoModelFactory,
    metaclass=BaseMetaFactory[models.ClusterSecurityGroup],
):
    class Meta:
        model = models.ClusterSecurityGroup

    name = factory.Sequence(lambda n: "security_group%s" % n)
    cluster = factory.SubFactory(ClusterFactory)

    @classmethod
    def get_url(cls, security_group=None):
        if security_group is None:
            security_group = ClusterSecurityGroupFactory()
        return "http://testserver" + reverse(
            "rancher-cluster-security-groups-detail",
            kwargs={
                "uuid": security_group.uuid.hex,
            },
        )
