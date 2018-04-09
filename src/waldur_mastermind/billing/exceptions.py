from __future__ import unicode_literals

from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, status


class PriceEstimateLimitExceeded(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, price_estimate):
        super(PriceEstimateLimitExceeded, self).__init__()
        message = _('Price for %(scope_type)s "%(scope_name)s" is over limit. Required: %(required)s, limit: %(limit)s')
        context = {
            'scope_type': price_estimate.content_type,
            'scope_name': price_estimate.scope.name,
            'required': price_estimate.total,
            'limit': price_estimate.limit,
        }
        self.detail = message % context

    def __str__(self):
        return self.detail
