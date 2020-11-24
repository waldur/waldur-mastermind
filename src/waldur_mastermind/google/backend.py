import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class GoogleAuthorize:
    '''
    https://developers.google.com/identity/protocols/oauth2/web-server
    '''

    def __init__(
        self, service_provider, redirect_uri, scopes=None,
    ):
        scopes = scopes or ['https://www.googleapis.com/auth/calendar.events']
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

    def __init__(self, tokens, calendar_id):
        self.tokens = tokens
        self.calendar_id = calendar_id

    @property
    def credentials(self):
        return Credentials(
            token=self.tokens.calendar_token,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=self.tokens.client_id,
            client_secret=self.tokens.client_secret,
            scopes=['https://www.googleapis.com/auth/calendar.events'],
            expiry=datetime.datetime.now(),
            refresh_token=self.tokens.calendar_refresh_token,
        )

    @property
    def service(self):
        return build(
            'calendar', 'v3', credentials=self.credentials, cache_discovery=False
        )

    def get_events(self, time_min=None, calendar_id='primary'):
        time_min = time_min or datetime.datetime.utcnow().isoformat() + 'Z'
        return (
            self.service.events()
            .list(calendarId=calendar_id, timeMin=time_min,)
            .execute()
            .get('items', [])
        )

    def create_event(
        self, summary, event_id, start, end, time_zone='GMT', calendar_id='primary'
    ):

        # check
        try:
            self.service.events().get(
                calendarId=calendar_id, eventId=event_id
            ).execute()
            self.update_event(summary, event_id, start, end, time_zone, calendar_id)
            return
        except HttpError:
            pass

        event_body = {
            'summary': summary,
            'id': event_id,
            'start': {'dateTime': start.isoformat(), 'timeZone': time_zone,},
            'end': {'dateTime': end.isoformat(), 'timeZone': time_zone,},
        }
        self.service.events().insert(calendarId=calendar_id, body=event_body).execute()

    def delete_event(self, event_id, calendar_id='primary'):
        self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    def update_event(
        self, summary, event_id, start, end, time_zone='GMT', calendar_id='primary'
    ):
        event_body = {
            'summary': summary,
            'start': {'dateTime': start.isoformat(), 'timeZone': time_zone,},
            'end': {'dateTime': end.isoformat(), 'timeZone': time_zone,},
            'status': 'confirmed',
        }
        self.service.events().update(
            calendarId=calendar_id, eventId=event_id, body=event_body
        ).execute()
