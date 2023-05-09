from django.conf import settings
from rest_framework.reverse import reverse

from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.support import views as support_views


def create_issue(offering_request):
    if not settings.WALDUR_SUPPORT['ENABLED']:
        return

    user = offering_request.requested_by
    post_data = {
        'summary': 'Request publishing of public offering',
        'caller': reverse('user-detail', kwargs={'uuid': user.uuid.hex}),
        'description': 'Please review and activate offering {offering_name} ({offering_uuid}). \n'
        'Requestor: {user_name} / {user_uuid}. \n'
        'Service provider: {customer_name} / {customer_uuid}'.format(
            offering_name=offering_request.offering.name,
            offering_uuid=offering_request.offering.uuid,
            user_name=user.full_name,
            user_uuid=user.uuid,
            customer_name=offering_request.offering.customer.name,
            customer_uuid=offering_request.offering.customer.uuid.hex,
        ),
        'type': settings.WALDUR_ATLASSIAN['DEFAULT_OFFERING_ISSUE_TYPE'],
    }

    return common_utils.create_request(
        support_views.IssueViewSet.as_view({'post': 'create'}),
        user,
        post_data,
    )
