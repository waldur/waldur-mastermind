import functools
from collections import namedtuple

from django.conf import settings
from requests import exceptions as requests_exceptions
from zammad_py import ZammadAPI

from waldur_core.core.clean_html import clean_html
from waldur_core.structure.exceptions import ServiceBackendError

ZAMMAD_API_URL = settings.WALDUR_ZAMMAD['ZAMMAD_API_URL']
ZAMMAD_TOKEN = settings.WALDUR_ZAMMAD['ZAMMAD_TOKEN']
ZAMMAD_ARTICLE_TYPE = settings.WALDUR_ZAMMAD['ZAMMAD_ARTICLE_TYPE']
ZAMMAD_GROUP = settings.WALDUR_ZAMMAD['ZAMMAD_GROUP']


class ZammadBackendError(ServiceBackendError):
    pass


def reraise_exceptions(msg=None):
    def wrap(func):
        @functools.wraps(func)
        def wrapped(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except requests_exceptions.HTTPError as e:
                raise ZammadBackendError(msg or e)

        return wrapped

    return wrap


Comment = namedtuple('Comment', 'id creator created content')
Issue = namedtuple('Issue', 'id status summary')


class ZammadBackend:
    def __init__(self):
        if not ZAMMAD_API_URL.endswith('/'):
            url = f'{ZAMMAD_API_URL}/api/v1/'
        else:
            url = f'{ZAMMAD_API_URL}api/v1/'

        self.manager = ZammadAPI(url, http_token=ZAMMAD_TOKEN)

    def _zammad_response_to_issue(self, response):
        return Issue(
            response['id'],
            response['state'],
            response['title'],
        )

    def _zammad_response_to_comment(self, response):
        return Comment(
            response.get('id', ''),
            response.get('created_by'),
            response.get('created_at'),
            clean_html(response.get('body', '')),
        )

    @reraise_exceptions('An issue is not found.')
    def get_issue(self, issue_id):
        response = self.manager.ticket.find(issue_id)
        return self._zammad_response_to_issue(response)

    def get_groups(self):
        return list(self.manager.group.all())

    def get_user(self, email):
        response = self.manager.user.search({'query': email})

        if response:
            return response[0]

    @reraise_exceptions('Creating an user has been failed.')
    def add_user(self, email):
        return self.manager.user.create({'login': email, 'email': email})

    @reraise_exceptions('Creating a comment has been failed.')
    def add_comment(self, ticket_id, content):
        response = self.manager.ticket_article.create(
            {
                'ticket_id': ticket_id,
                'body': content,
            }
        )

        return self._zammad_response_to_comment(response)

    @reraise_exceptions('Creating an issue has been failed.')
    def add_issue(self, subject, description, email, group=None):
        group = group or ZAMMAD_GROUP or self.get_groups()[0]['name']

        if not self.get_user(email):
            self.add_user(email)

        response = self.manager.ticket.create(
            {
                'title': subject,
                'customer': email,
                'group': group,
                "article": {
                    "subject": "Task description",
                    "body": description,
                    "type": ZAMMAD_ARTICLE_TYPE,
                    "internal": False,
                },
            }
        )

        return self._zammad_response_to_issue(response)

    @reraise_exceptions('Comments have not been got because an issue is not found.')
    def get_comments(self, ticket_id):
        comments = []

        for zammad_comment in self.manager.ticket.articles(ticket_id):
            comment = self._zammad_response_to_comment(zammad_comment)
            comments.append(comment)

        return comments
