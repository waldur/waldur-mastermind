from django.utils.functional import cached_property
from rest_framework.exceptions import ValidationError

from waldur_mastermind.marketplace import processors
from waldur_mastermind.marketplace_remote.utils import get_client_for_offering


class RemoteClientMixin:
    @cached_property
    def client(self):
        return get_client_for_offering(self.order_item.offering)

    def get_or_create_project(self):
        options = self.order_item.offering.secret_options
        local_project = self.order_item.order.project
        remote_customer_uuid = options['customer_uuid']
        remote_project_name = f'{local_project.customer.name} / {local_project.name}'
        remote_project_uuid = f'{local_project.customer.uuid}_{local_project.uuid}'
        remote_projects = self.client.list_projects(
            query_params={'backend_id': remote_project_uuid}
        )
        if len(remote_projects) == 0:
            return self.client.create_project(
                customer_uuid=remote_customer_uuid,
                name=remote_project_name,
                backend_id=remote_project_uuid,
            )
        elif len(remote_projects) == 1:
            return remote_projects[0]
        else:
            raise ValidationError('There are multiple projects in remote Waldur.')


class RemoteCreateResourceProcessor(
    RemoteClientMixin, processors.BasicCreateResourceProcessor
):
    def send_request(self, user):
        remote_project = self.get_or_create_project()
        response = self.client.marketplace_resource_create(
            project_uuid=remote_project['uuid'],
            offering_uuid=self.order_item.offering.backend_id,
            plan_uuid=self.order_item.plan.backend_id,
            attributes=self.order_item.attributes,
            limits=self.order_item.limits,
        )
        self.order_item.backend_id = response['uuid']
        self.order_item.save()


class RemoteUpdateResourceProcessor(
    RemoteClientMixin, processors.BasicUpdateResourceProcessor
):
    def update_limits_process(self, user):
        response = self.client.marketplace_resource_update_limits(
            self.order_item.resource.backend_id, self.order_item.limits,
        )
        self.order_item.backend_id = response['uuid']
        self.order_item.save()


class RemoteDeleteResourceProcessor(
    RemoteClientMixin, processors.BasicDeleteResourceProcessor
):
    def send_request(self, user, resource):
        response = self.client.marketplace_resource_terminate(
            self.order_item.resource.backend_id
        )
        self.order_item.backend_id = response['uuid']
        self.order_item.save()
        return True
