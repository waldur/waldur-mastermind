import functools
import re
from dataclasses import dataclass
from typing import List

from constance import config
from requests import exceptions as requests_exceptions
from zammad_py import ZammadAPI

from waldur_core.core.clean_html import clean_html
from waldur_core.structure.exceptions import ServiceBackendError


class ZammadBackendError(ServiceBackendError):
    pass


def reraise_exceptions(msg=None):
    def wrap(func):
        @functools.wraps(func)
        def wrapped(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except requests_exceptions.HTTPError as e:
                raise ZammadBackendError(f"{msg}. {e}")

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
        if not config.ZAMMAD_API_URL.endswith("/"):
            url = f"{config.ZAMMAD_API_URL}/api/v1/"
        else:
            url = f"{config.ZAMMAD_API_URL}api/v1/"

        self.manager = ZammadAPI(url, http_token=config.ZAMMAD_TOKEN)

    def _zammad_response_to_issue(self, response):
        return Issue(
            id=str(response["id"]),
            status=response["state"],
            summary=response["title"],
        )

    def _zammad_response_to_user(self, response):
        if response["firstname"] and response["lastname"]:
            name = "{} {}".format(response["firstname"], response["lastname"])
        else:
            name = response["login"]

        return User(
            id=response["id"],
            email=response["email"],
            login=response["login"],
            firstname=response["firstname"],
            lastname=response["lastname"],
            name=name,
            is_active=response["active"],
        )

    def _zammad_response_to_comment(self, response):
        article_id = str(response.get("id"))
        ticket_id = str(response.get("ticket_id"))
        return Comment(
            id=article_id,
            creator=response.get("from"),
            created=response.get("created_at"),
            content=clean_html(response.get("body", "")),
            is_public=not response.get("internal", False),
            user_id=response.get("created_by_id"),
            is_waldur_comment=re.search(
                r"^" + config.ZAMMAD_COMMENT_MARKER,
                clean_html(response.get("body", "")),
                re.MULTILINE,
            ),
            attachments=[
                self._zammad_response_to_attachment(a, article_id, ticket_id)
                for a in response.get("attachments", [])
            ],
            ticket_id=ticket_id,
        )

    def _zammad_response_to_attachment(self, response, article_id, ticket_id):
        return Attachment(
            id=str(response.get("id")),
            filename=response.get("filename"),
            size=response.get("size"),
            content_type=response.get("preferences", {}).get("Content-Type")
            or response.get("preferences", {}).get("Mime-Type"),
            article_id=article_id,
            ticket_id=ticket_id,
        )

    def _zammad_response_to_priority(self, response):
        return Priority(
            id=str(response.get("id")),
            name=response.get("name"),
        )

    @reraise_exceptions("An issue is not found.")
    def get_issue(self, issue_id):
        response = self.manager.ticket.find(issue_id)
        return self._zammad_response_to_issue(response)

    @reraise_exceptions("A comment is not found.")
    def get_comment(self, comment_id):
        response = self.manager.ticket_article.find(comment_id)
        return self._zammad_response_to_comment(response)

    def get_groups(self):
        return list(self.manager.group.all())

    def get_user_by_field(self, value, field_name):
        """ " value: email, login, firstname, lastname and so on."""
        response = self.manager.user.search({"query": value})

        if len(response) == 1:
            return self._zammad_response_to_user(response[0])
        elif len(response) > 1:
            result = [r for r in response if r[field_name] == value]
            if len(result) == 1:
                return self._zammad_response_to_user(result[0])

    def get_user_by_login(self, login):
        return self.get_user_by_field(login, "login")

    def get_user_by_email(self, email):
        return self.get_user_by_field(email, "email")

    @reraise_exceptions("A user is not found.")
    def get_user_by_id(self, user_id):
        response = self.manager.user.find(user_id)
        return self._zammad_response_to_user(response)

    def get_users(self):
        response = self.manager.user._connection.session.get(
            self.manager.user.url, params={}
        )
        json_response = self.manager.user._raise_or_return_json(response)
        return [self._zammad_response_to_user(r) for r in json_response]

    @reraise_exceptions("Creating a user has failed.")
    def add_user(self, login, email, firstname, lastname):
        response = self.manager.user.create(
            {
                "login": login,
                "email": email,
                "firstname": firstname,
                "lastname": lastname,
                "roles": [
                    "Agent",
                    "Customer",
                ],
            }
        )
        return self._zammad_response_to_user(response)

    @reraise_exceptions("Creating a comment has failed.")
    def add_comment(
        self,
        ticket_id,
        content,
        is_public=False,
        support_user_name=None,
        zammad_user_email=None,
    ):
        params = {
            "ticket_id": ticket_id,
            "body": content
            + "\n\n"
            + config.ZAMMAD_COMMENT_MARKER
            + "\n\n"
            + config.ZAMMAD_COMMENT_PREFIX.format(name=support_user_name),
            "type": config.ZAMMAD_ARTICLE_TYPE,
            "internal": not is_public,  # if internal equals False so deleting of comment will be impossible
        }

        if zammad_user_email:
            params["to"] = zammad_user_email

        response = self.manager.ticket_article.create(params)
        return self._zammad_response_to_comment(response)

    def get_ticket_attachments(self, ticket_id):
        attachments = []

        for comment in self.get_comments(ticket_id):
            attachments.extend(comment.attachments)

        return attachments

    @reraise_exceptions("Creating an attachment has failed.")
    def add_attachment(
        self,
        ticket_id,
        filename,
        data_base64_encoded,
        mime_type,
        waldur_user_email,
        author_name="",
    ):
        body = filename

        if author_name:
            body = f"User {author_name} added file {filename}."

        params = {
            "ticket_id": ticket_id,
            "body": body + "\n\n" + config.ZAMMAD_COMMENT_MARKER,
            "to": waldur_user_email,
            "type": config.ZAMMAD_ARTICLE_TYPE,
            "internal": True,  # if internal equals False so deleting of comment will be impossible
            "attachments": [
                {
                    "filename": filename,
                    "data": data_base64_encoded,
                    "mime-type": mime_type,
                }
            ],
        }

        response = self.manager.ticket_article.create(params)
        zammad_comment = self._zammad_response_to_comment(response)
        return zammad_comment.attachments[0]

    @reraise_exceptions("Deleting a comment has failed.")
    def del_comment(self, comment_id):
        self.manager.ticket_article.destroy(comment_id)
        return

    @reraise_exceptions("Creating an issue has failed.")
    def add_issue(
        self,
        subject,
        description,
        customer_id,
        waldur_user_email,
        group=None,
        tags: List[str] = "",
    ):
        group = group or config.ZAMMAD_GROUP or self.get_groups()[0]["name"]
        tags = ",".join(tags)
        params = {
            "title": subject,
            "customer_id": customer_id,
            "group": group,
            "article": {
                "subject": "Task description",
                "body": description,  # We do not add marker as we treat first article as a special one.
                "type": config.ZAMMAD_ARTICLE_TYPE,
                "internal": False,
                "to": waldur_user_email,
            },
        }

        if tags:
            params["tags"] = tags

        response = self.manager.ticket.create(params)

        return self._zammad_response_to_issue(response)

    @reraise_exceptions(
        "Comments have not been received as referenced issue has not been found."
    )
    def get_comments(self, ticket_id):
        comments = []

        for zammad_comment in self.manager.ticket.articles(ticket_id):
            if zammad_comment["sender"] == "System":
                continue
            comment = self._zammad_response_to_comment(zammad_comment)
            comments.append(comment)

        # If ticket has been created via Waldur then first comment is task description
        comments[0].is_waldur_comment = True

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
        self.manager.ticket.update(issue_id, {"title": title})

    def pull_priorities(self):
        return [
            self._zammad_response_to_priority(p)
            for p in self.manager.ticket_priority.all()
        ]
