from __future__ import unicode_literals

from collections import OrderedDict

from rest_framework import pagination
from rest_framework.response import Response
from rest_framework.utils.urls import remove_query_param, replace_query_param


class LinkHeaderPagination(pagination.PageNumberPagination):
    page_size_query_param = 'page_size'
    max_page_size = 300

    def get_paginated_response(self, data):
        link_candidates = OrderedDict((
            ('first', self.get_first_link),
            ('prev', self.get_previous_link),
            ('next', self.get_next_link),
            ('last', self.get_last_link),
        ))

        link = ', '.join(
            '<%s>; rel="%s"' % (get_link(), rel)
            for rel, get_link in link_candidates.items()
            if get_link()
        )

        headers = {
            'X-Result-Count': self.page.paginator.count,
            'Link': link,
        }

        return Response(data, headers=headers)

    def get_first_link(self):
        url = self.request.build_absolute_uri()
        return remove_query_param(url, self.page_query_param)

    def get_last_link(self):
        url = self.request.build_absolute_uri()
        page_number = self.page.paginator.page_range[-1]
        if page_number == 1:
            return remove_query_param(url, self.page_query_param)
        return replace_query_param(url, self.page_query_param, page_number)


class UnlimitedLinkHeaderPagination(LinkHeaderPagination):
    """
    A hackish paginator for cases when calculating a queryset to display is an expensive query and if
    once the result set is known, it's cheaper to serialize larger output.

    Should be used only as a temporary workaround!
    """
    page_size = None
