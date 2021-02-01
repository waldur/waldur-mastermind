from waldur_mastermind.marketplace import processors

from .views import IssueViewSet


class CreateRequestProcessor(processors.BaseCreateResourceProcessor):
    viewset = IssueViewSet

    def get_post_data(self):
        return {'uuid': str(self.order_item.uuid)}


class DeleteRequestProcessor(processors.DeleteResourceProcessor):
    viewset = IssueViewSet

    def get_resource(self):
        return self.order_item


class UpdateRequestProcessor(processors.UpdateResourceProcessor):
    def get_view(self):
        return IssueViewSet.as_view({'post': 'update'})

    def get_post_data(self):
        return {'uuid': str(self.order_item.uuid)}

    def get_resource(self):
        return self.order_item.resource
