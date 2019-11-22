from waldur_mastermind.marketplace import processors

from .utils import DockerExecutorMixin


class CreateProcessor(DockerExecutorMixin, processors.AbstractCreateResourceProcessor):
    script_name = 'create_script'


class UpdateProcessor(DockerExecutorMixin, processors.AbstractUpdateResourceProcessor):
    script_name = 'update_script'


class DeleteProcessor(DockerExecutorMixin, processors.AbstractDeleteResourceProcessor):
    script_name = 'delete_script'
