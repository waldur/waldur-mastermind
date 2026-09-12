from waldur_azure import views as azure_views
from waldur_mastermind.marketplace import processors


class VirtualMachineCreateProcessor(processors.BaseCreateResourceProcessor):
    viewset = azure_views.VirtualMachineViewSet
    fields = (
        "name",
        "description",
        "size",
        "image",
        "location",
    )


class VirtualMachineDeleteProcessor(processors.DeleteScopedResourceProcessor):
    viewset = azure_views.VirtualMachineViewSet
