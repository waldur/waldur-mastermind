from __future__ import unicode_literals

import uuid

from django.db.models import Q
import django_filters
from django_filters.rest_framework import DjangoFilterBackend

from waldur_core.core import filters as core_filters
from waldur_core.users import models


class InvitationFilter(django_filters.FilterSet):
    project = django_filters.UUIDFilter(
        name='project__uuid',
    )
    project_url = core_filters.URLFilter(
        view_name='project-detail',
        name='project__uuid',
    )
    state = django_filters.MultipleChoiceFilter(choices=models.Invitation.State.CHOICES)

    o = django_filters.OrderingFilter(fields=('email', 'state', 'created'))

    class Meta(object):
        model = models.Invitation
        fields = [
            'email',
            'civil_number',
            'customer_role',
            'project_role',
        ]


class InvitationCustomerFilterBackend(DjangoFilterBackend):
    url_filter = core_filters.URLFilter(
        view_name='customer-detail',
        name='customer__uuid',
    )

    def filter_queryset(self, request, queryset, view):
        customer_uuid = self.extract_customer_uuid(request)
        if not customer_uuid:
            return queryset

        try:
            uuid.UUID(customer_uuid)
        except ValueError:
            return queryset.none()

        query = Q(customer__uuid=customer_uuid)
        query |= Q(project__customer__uuid=customer_uuid)
        return queryset.filter(query)

    def extract_customer_uuid(self, request):
        if 'customer_url' in request.query_params:
            return self.url_filter.get_uuid(request.query_params['customer_url'])

        if 'customer' in request.query_params:
            return request.query_params['customer']
