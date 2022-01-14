from waldur_mastermind.marketplace import processors

from .utils import ContainerExecutorMixin


"""
It is expected that offering plugin_options field is dict with following structure:

language: python

environ:
    USERNAME: admin
    PASSWORD: secret

create:
    import os
    print("Creating resource ", os.environ.get('RESOURCE_NAME'))

update:
    import os
    print("Updating resource ", os.environ.get('RESOURCE_NAME'))

delete:
    import os
    print("Deleting resource ", os.environ.get('RESOURCE_NAME'))

pull:
    import os
    print("Pulling resource ", os.environ.get('RESOURCE_NAME'))
"""


class CreateProcessor(
    ContainerExecutorMixin, processors.AbstractCreateResourceProcessor
):
    hook_type = 'create'

    def send_request(self, user):
        output = super().send_request(user)
        # return the last line of the output as a backend_id of a created resource
        if output:
            return output.splitlines()[-1]


class UpdateProcessor(
    ContainerExecutorMixin, processors.AbstractUpdateResourceProcessor
):
    hook_type = 'update'

    def send_request(self, user):
        super().send_request(user)
        return True


class DeleteProcessor(
    ContainerExecutorMixin, processors.AbstractDeleteResourceProcessor
):
    hook_type = 'delete'

    def send_request(self, user, resource):
        super().send_request(user, resource)
        return True
