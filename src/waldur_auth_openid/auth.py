from __future__ import unicode_literals

import logging
import uuid

from django.conf import settings
from django_openid_auth.auth import OpenIDBackend


logger = logging.getLogger(__name__)


class WaldurOpenIDBackend(OpenIDBackend):
    """ This backend sets user's full_name and email. """

    def update_user_details(self, user, details, openid_response):
        updated_fields = []

        # Don't update full_name if it is already set
        if not user.full_name:
            user.full_name = '{} {}'.format(details['first_name'], details['last_name']).strip()
            updated_fields.append('full_name')

        # Don't update email if it is already set
        if not user.email and details['email']:
            user.email = details['email']
            updated_fields.append('email')

        # Civil number should be updated after each login because it can be changed or
        # defined for user.
        civil_number = self._get_civil_number(openid_response)
        if civil_number and user.civil_number != civil_number:
            user.civil_number = civil_number
            updated_fields.append('civil_number')

        if updated_fields:
            user.save(update_fields=updated_fields)

    def create_user_from_openid(self, openid_response):
        user = super(WaldurOpenIDBackend, self).create_user_from_openid(openid_response)
        civil_number = self._get_civil_number(openid_response)
        if civil_number and user.civil_number != civil_number:
            user.civil_number = civil_number
        user.registration_method = settings.WALDUR_AUTH_OPENID.get('NAME', 'openid')

        user.save()
        return user

    def _get_preferred_username(self, nickname, email):
        return uuid.uuid4().hex[:30]

    def _get_civil_number(self, openid_response):
        """
        Extract civil number from OpenID response.
        Return empty string if personal code is not defined.

        Expected openid.identity value: https://openid.ee/i/EE:<personal_code>
        Example: https://openid.ee/i/EE:37605030299
        Only the last part (<personal_code>) is stored as civil number.
        """
        openid_identity = openid_response.getSigned('http://specs.openid.net/auth/2.0', 'identity')

        personal_code = openid_identity.split('/')[-1].split(':')[-1]
        if personal_code.isdigit():
            return personal_code
        else:
            logger.warning(
                'Unable to parse openid.identity {}: personal code is not a numeric value'.format(openid_identity))
            return ''
