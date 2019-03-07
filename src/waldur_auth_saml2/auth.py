from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from djangosaml2.backends import Saml2Backend

User = get_user_model()


class WaldurSaml2Backend(Saml2Backend):
    def is_authorized(self, attributes, attribute_mapping):
        email = self.get_attribute_value('email', attributes, attribute_mapping)
        username = self.get_attribute_value('username', attributes, attribute_mapping)
        if email and User.objects.filter(email=email).exclude(username=username).exists():
            raise ValidationError('User with this email already exists')
        return True
