import functools
from dataclasses import dataclass
from typing import List

from django.conf import settings
from requests import exceptions as requests_exceptions
from zammad_py import ZammadAPI

from waldur_core.core.clean_html import clean_html
from waldur_core.structure.exceptions import ServiceBackendError

ZAMMAD_API_URL = settings.WALDUR_ZAMMAD['ZAMMAD_API_URL']
ZAMMAD_TOKEN = settings.WALDUR_ZAMMAD['ZAMMAD_TOKEN']
ZAMMAD_ARTICLE_TYPE = settings.WALDUR_ZAMMAD['ZAMMAD_ARTICLE_TYPE']
ZAMMAD_GROUP = settings.WALDUR_ZAMMAD['ZAMMAD_GROUP']
ZAMMAD_COMMENT_MARKER = settings.WALDUR_ZAMMAD['ZAMMAD_COMMENT_MARKER']


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


@dataclass
class Issue:
    id: str
    status: str
    summary: str


@dataclass
class Comment:
    id: str
    creator: str
    created: str
    content: str
    is_public: bool
    user_id: str
    is_waldur_comment: bool
    ticket_id: str
    attachments: list = None


@dataclass
class Attachment:
    id: str
    filename: str
    size: str
    content_type: str
    article_id: str
    ticket_id: str


@dataclass
class User:
    id: str
    email: str
    login: str
    firstname: str
    lastname: str
    name: str
    is_active: bool


@dataclass
class Priority:
    id: str
    name: str


class ZammadBackend:
    def __init__(self):
        if not ZAMMAD_API_URL.endswith('/'):
            url = f'{ZAMMAD_API_URL}/api/v1/'
        else:
            url = f'{ZAMMAD_API_URL}api/v1/'

        self.manager = ZammadAPI(url, http_token=ZAMMAD_TOKEN)

    def _zammad_response_to_issue(self, response):
        return Issue(
            id=str(response['id']),
            status=response['state'],
            summary=response['title'],
        )

    def _zammad_response_to_user(self, response):
        if response['firstname'] and response['lastname']:
            name = '{} {}'.format(response['firstname'], response['lastname'])
        else:
            name = response['login']

        return User(
            id=response['id'],
            email=response['email'],
            login=response['login'],
            firstname=response['firstname'],
            lastname=response['lastname'],
            name=name,
            is_active=response['active'],
        )

    def _zammad_response_to_comment(self, response):
        article_id = str(response.get('id'))
        ticket_id = str(response.get('ticket_id'))
        return Comment(
            id=article_id,
            creator=response.get('created_by'),
            created=response.get('created_at'),
            content=clean_html(response.get('body', '')),
            is_public=not response.get('internal', False),
            user_id=response.get('sender_id'),
            is_waldur_comment=ZAMMAD_COMMENT_MARKER
            in clean_html(response.get('body', '')),
            attachments=[
                self._zammad_response_to_attachment(a, article_id, ticket_id)
                for a in response.get('attachments', [])
            ],
            ticket_id=ticket_id,
        )

    def _zammad_response_to_attachment(self, response, article_id, ticket_id):
        return Attachment(
            id=str(response.get('id')),
            filename=response.get('filename'),
            size=response.get('size'),
            content_type=response.get('preferences', {}).get('Content-Type'),
            article_id=article_id,
            ticket_id=ticket_id,
        )

    def _zammad_response_to_priority(self, response):
        return Priority(
            id=str(response.get('id')),
            name=response.get('name'),
        )

    @reraise_exceptions('An issue is not found.')
    def get_issue(self, issue_id):
        response = self.manager.ticket.find(issue_id)
        return self._zammad_response_to_issue(response)

    @reraise_exceptions('A comment is not found.')
    def get_comment(self, comment_id):
        response = self.manager.ticket_article.find(comment_id)
        return self._zammad_response_to_comment(response)

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

    @reraise_exceptions('A user is not found.')
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
                'roles': [
                    'Agent',
                    'Customer',
                ],
            }
        )
        return self._zammad_response_to_user(response)

    @reraise_exceptions('Creating a comment has been failed.')
    def add_comment(self, ticket_id, content, is_public=False, zammad_user_id=None):
        if zammad_user_id:
            self.manager.on_behalf_of = str(zammad_user_id)

        params = {
            'ticket_id': ticket_id,
            'body': content + '\n\n' + ZAMMAD_COMMENT_MARKER,
            'type': ZAMMAD_ARTICLE_TYPE,
            'internal': not is_public,  # if internal equals False so deleting of comment will be impossible
        }
        response = self.manager.ticket_article.create(params)
        return self._zammad_response_to_comment(response)

    def get_ticket_attachments(self, ticket_id):
        attachments = []

        for comment in self.get_comments(ticket_id):
            attachments.extend(comment.attachments)

        return attachments

    @reraise_exceptions('Creating an attachment has been failed.')
    def add_attachment(
        self, ticket_id, filename, data_base64_encoded, mime_type, author_name=''
    ):
        body = filename

        if author_name:
            body = f'User {author_name} added file {filename}.'

        params = {
            'ticket_id': ticket_id,
            'body': body + '\n\n' + ZAMMAD_COMMENT_MARKER,
            'type': ZAMMAD_ARTICLE_TYPE,
            'internal': True,  # if internal equals False so deleting of comment will be impossible
            'attachments': [
                {
                    'filename': filename,
                    'data': data_base64_encoded,
                    'mime-type': mime_type,
                }
            ],
        }

        response = self.manager.ticket_article.create(params)
        zammad_comment = self._zammad_response_to_comment(response)
        return zammad_comment.attachments[0]

    @reraise_exceptions('Deleting a comment has failed.')
    def del_comment(self, comment_id):
        self.manager.ticket_article.destroy(comment_id)
        return

    @reraise_exceptions('Creating an issue has failed.')
    def add_issue(
        self, subject, description, customer_id, group=None, tags: List[str] = ''
    ):
        group = group or ZAMMAD_GROUP or self.get_groups()[0]['name']
        tags = ','.join(tags)
        params = {
            'title': subject,
            'customer_id': customer_id,
            'group': group,
            "article": {
                "subject": "Task description",
                "body": description + '\n\n' + ZAMMAD_COMMENT_MARKER,
                "type": ZAMMAD_ARTICLE_TYPE,
                "internal": False,
            },
        }

        if tags:
            params['tags'] = tags

        response = self.manager.ticket.create(params)

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

    def attachment_download(self, attachment):
        return self.manager.ticket_article_attachment.download(
            attachment.id,
            attachment.article_id,
            attachment.ticket_id,
        )

    def delete_issue(self, issue_id):
        self.manager.ticket.destroy(issue_id)

    def update_issue(self, issue_id, title):
        self.manager.ticket.update(issue_id, {'title': title})

    def pull_priorities(self):
        return [
            self._zammad_response_to_priority(p)
            for p in self.manager.ticket_priority.all()
        ]
