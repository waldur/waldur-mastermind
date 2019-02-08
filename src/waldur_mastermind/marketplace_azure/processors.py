from waldur_azure import views as azure_views
from waldur_mastermind.marketplace import processors


class VirtualMachineCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = azure_views.VirtualMachineViewSet
    fields = (
        'name',
        'description',
        'size',
        'image',
        'location',
    )


class VirtualMachineDeleteProcessor(processors.DeleteResourceProcessor):
    viewset = azure_views.VirtualMachineViewSet


class SQLServerCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = azure_views.SQLServerViewSet

    fields = (
        'name',
        'description',
        'location',
    )


class SQLServerDeleteProcessor(processors.DeleteResourceProcessor):
    viewset = azure_views.SQLServerViewSet
