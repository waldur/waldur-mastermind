from waldur_ansible.common.tests.integration.container import DockerContainer
from waldur_ansible.common.tests.integration.ubuntu1604_image import Ubuntu1604Image

CONTAINER_SSH_PORT_ON_HOST = '2222'


class Ubuntu1604Container(DockerContainer):
    def __init__(self):
        super(Ubuntu1604Container, self).__init__("integration-test-ubuntu1604-container", Ubuntu1604Image.IMAGE_NAME)
        self.bind_port('22/tcp', CONTAINER_SSH_PORT_ON_HOST)
