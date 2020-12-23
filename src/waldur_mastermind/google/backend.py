import datetime
import functools

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from waldur_core.structure import ServiceBackendError


class GoogleBackendError(ServiceBackendError):
    pass


def reraise_exceptions(func):
    @functools.wraps(func)
    def wrapped(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except HttpError as e:
            raise GoogleBackendError(e)

    return wrapped


SCOPES = ['https://www.googleapis.com/auth/calendar']


class GoogleAuthorize:
    '''
    https://developers.google.com/identity/protocols/oauth2/web-server
    '''

    def __init__(
        self, service_provider, redirect_uri, scopes=None,
    ):
        scopes = scopes or SCOPES
        self.service_provider = service_provider
        self.credentials = service_provider.googlecredentials
        self.flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": self.credentials.client_id,
                    "project_id": self.credentials.project_id,
                    "client_secret": self.credentials.client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                }
            },
            scopes,
        )
        self.flow.redirect_uri = redirect_uri

    def get_authorization_url(self):
        auth_url, _ = self.flow.authorization_url(prompt='consent')
        return auth_url

    def create_tokens(self, code):
        tokens = self.flow.fetch_token(code=code)
        self.credentials.calendar_token = tokens.get('access_token')
        self.credentials.calendar_refresh_token = tokens.get('refresh_token')
        self.credentials.save()


class GoogleCalendar:
    '''
    API docs: https://developers.google.com/calendar/v3/reference/
    '''

    def __init__(self, tokens):
        self.tokens = tokens

    @property
    def credentials(self):
        return Credentials(
            token=self.tokens.calendar_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=self.tokens.client_id,
            client_secret=self.tokens.client_secret,
            scopes=SCOPES,
            expiry=datetime.datetime.now(),
            refresh_token=self.tokens.calendar_refresh_token,
        )

    @property
    def service(self):
        return build(
            'calendar', 'v3', credentials=self.credentials, cache_discovery=False
        )

    @reraise_exceptions
    def get_events(self, calendar_id='primary'):
        return (
            self.service.events()
            .list(calendarId=calendar_id)
            .execute()
            .get('items', [])
        )

    @reraise_exceptions
    def create_event(
        self,
        summary,
        event_id,
        start,
        end,
        time_zone='GMT',
        calendar_id='primary',
        location=None,
        attendees=None,
    ):

        # check
        try:
            self.service.events().get(
                calendarId=calendar_id, eventId=event_id
            ).execute()
            self.update_event(
                summary,
                event_id,
                start,
                end,
                time_zone,
                calendar_id,
                location,
                attendees,
            )
            return
        except HttpError:
            pass

        event_body = {
            'summary': summary,
            'id': event_id,
            'start': {'dateTime': start.isoformat(), 'timeZone': time_zone},
            'end': {'dateTime': end.isoformat(), 'timeZone': time_zone},
            'location': location,
            'attendees': attendees or [],
        }
        self.service.events().insert(calendarId=calendar_id, body=event_body).execute()

    @reraise_exceptions
    def delete_event(self, event_id, calendar_id='primary'):
        self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    @reraise_exceptions
    def update_event(
        self,
        summary,
        event_id,
        start,
        end,
        time_zone='GMT',
        calendar_id='primary',
        location=None,
        attendees=None,
    ):
        event_body = {
            'summary': summary,
            'start': {'dateTime': start.isoformat(), 'timeZone': time_zone},
            'end': {'dateTime': end.isoformat(), 'timeZone': time_zone},
            'status': 'confirmed',
            'location': location,
            'attendees': attendees or [],
        }
        self.service.events().update(
            calendarId=calendar_id, eventId=event_id, body=event_body
        ).execute()

    @reraise_exceptions
    def create_calendar(self, calendar_name, time_zone='GMT'):
        body = {'summary': calendar_name, 'timeZone': time_zone}
        calendar = self.service.calendars().insert(body=body).execute()
        return calendar['id']

    @reraise_exceptions
    def delete_calendar(self, calendar_id):
        self.service.calendars().delete(calendarId=calendar_id).execute()

    @reraise_exceptions
    def update_calendar(self, calendar_id, summary=None, time_zone=None, location=None):
        body = {}

        if summary:
            body['summary'] = summary

        if time_zone:
            body['timeZone'] = time_zone

        if location:
            body['location'] = location

        if body:
            self.service.calendars().update(calendarId=calendar_id, body=body).execute()

    @reraise_exceptions
    def share_calendar(self, calendar_id):
        rule = {'scope': {'type': 'default'}, 'role': 'reader'}

        self.service.acl().insert(calendarId=calendar_id, body=rule).execute()

    @reraise_exceptions
    def unshare_calendar(self, calendar_id):
        self.service.acl().delete(calendarId=calendar_id, ruleId='default').execute()
