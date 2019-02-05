from waldur_azure import models as azure_models
from waldur_azure import views as azure_views
from waldur_mastermind.marketplace.utils import \
    CreateResourceProcessor, DeleteResourceProcessor, get_spl_url, copy_attributes


class AzureCreateResourceProcessor(CreateResourceProcessor):
    """
    Abstract base class to adapt Azure resource provisioning endpoints to marketplace API.
    """
    def get_serializer_class(self):
        return self.get_viewset().serializer_class

    def get_viewset(self):
        raise NotImplementedError

    def get_fields(self):
        raise NotImplementedError

    def get_post_data(self):
        order_item = self.order_item
        return dict(
            service_project_link=get_spl_url(azure_models.AzureServiceProjectLink, order_item),
            **copy_attributes(self.get_fields(), order_item)
        )

    def get_scope_from_response(self, response):
        return self.get_viewset().queryset.model.objects.get(uuid=response.data['uuid'])


class VirtualMachineCreateProcessor(AzureCreateResourceProcessor):
    def get_viewset(self):
        return azure_views.VirtualMachineViewSet

    def get_fields(self):
        return (
            'name',
            'description',
            'size',
            'image',
            'location',
        )


class VirtualMachineDeleteProcessor(DeleteResourceProcessor):
    def get_viewset(self):
        return azure_views.VirtualMachineViewSet


class SQLServerCreateProcessor(AzureCreateResourceProcessor):
    def get_viewset(self):
        return azure_views.SQLServerViewSet

    def get_post_data(self):
        return (
            'name',
            'description',
            'location',
        )


class SQLServerDeleteProcessor(DeleteResourceProcessor):
    def get_viewset(self):
        return azure_views.SQLServerViewSet


class SQLDatabaseCreateProcessor(AzureCreateResourceProcessor):
    def get_viewset(self):
        return azure_views.SQLDatabaseViewSet

    def get_post_data(self):
        return (
            'name',
            'description',
            'server',
        )


class SQLDatabaseDeleteProcessor(DeleteResourceProcessor):
    def get_viewset(self):
        return azure_views.SQLDatabaseViewSet
