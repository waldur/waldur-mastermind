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


Comment = namedtuple(
    'Comment', 'id creator created content is_public user_id is_waldur_comment'
)
Issue = namedtuple('Issue', 'id status summary')
User = namedtuple('User', 'id email login firstname lastname name is_active')


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

    def _zammad_response_to_user(self, response):
        if response['firstname'] and response['lastname']:
            name = '{} {}'.format(response['firstname'], response['lastname'])
        else:
            name = response['login']

        return User(
            response['id'],
            response['email'],
            response['login'],
            response['firstname'],
            response['lastname'],
            name,
            response['active'],
        )

    def _zammad_response_to_comment(self, response):
        return Comment(
            response.get('id', ''),
            response.get('created_by'),
            response.get('created_at'),
            clean_html(response.get('body', '')),
            not response.get('internal', False),
            response.get('sender_id'),
            response.get('type') == ZAMMAD_ARTICLE_TYPE,
        )

    @reraise_exceptions('An issue is not found.')
    def get_issue(self, issue_id):
        response = self.manager.ticket.find(issue_id)
        return self._zammad_response_to_issue(response)

    def get_groups(self):
        return list(self.manager.group.all())

    def get_user_by_field(self, value, field_name):
        """ " value: email, login, firstname, lastname and so on."""
        response = self.manager.user.search({'query': value})

        if len(response) == 1:
            return self._zammad_response_to_user(response[0])
        elif len(response) > 1:
            result = [r for r in response if r[field_name] == value]
            if len(result) == 1:
                return self._zammad_response_to_user(result[0])

    def get_user_by_login(self, login):
        return self.get_user_by_field(login, 'login')

    def get_user_by_email(self, email):
        return self.get_user_by_field(email, 'email')

    @reraise_exceptions('An user is not found.')
    def get_user_by_id(self, user_id):
        response = self.manager.user.find(user_id)
        return self._zammad_response_to_user(response)

    def get_users(self):
        response = self.manager.user._connection.session.get(
            self.manager.user.url, params={}
        )
        json_response = self.manager.user._raise_or_return_json(response)
        return [self._zammad_response_to_user(r) for r in json_response]

    @reraise_exceptions('Creating an user has been failed.')
    def add_user(self, login, email, firstname, lastname):
        response = self.manager.user.create(
            {
                'login': login,
                'email': email,
                'firstname': firstname,
                'lastname': lastname,
            }
        )
        return self._zammad_response_to_user(response)

    @reraise_exceptions('Creating a comment has been failed.')
    def add_comment(self, ticket_id, content):
        params = {
            'ticket_id': ticket_id,
            'body': content,
            'type': ZAMMAD_ARTICLE_TYPE,
        }
        response = self.manager.ticket_article.create(params)
        return self._zammad_response_to_comment(response)

    @reraise_exceptions('Deleting a comment has failed.')
    def del_comment(self, comment_id):
        self.manager.ticket_article.destroy(comment_id)
        return

    @reraise_exceptions('Creating an issue has failed.')
    def add_issue(self, subject, description, customer_id, group=None):
        group = group or ZAMMAD_GROUP or self.get_groups()[0]['name']
        response = self.manager.ticket.create(
            {
                'title': subject,
                'customer_id': customer_id,
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

    @reraise_exceptions(
        'Comments have not been received as referenced issue has not been found.'
    )
    def get_comments(self, ticket_id):
        comments = []

        for zammad_comment in self.manager.ticket.articles(ticket_id):
            comment = self._zammad_response_to_comment(zammad_comment)
            comments.append(comment)

        return comments
