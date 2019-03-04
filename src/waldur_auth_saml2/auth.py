from django.core.exceptions import ValidationError
from djangosaml2.backends import Saml2Backend, get_saml_user_model


class WaldurSaml2Backend(Saml2Backend):
    def get_saml2_user(self, create, main_attribute, attributes, attribute_mapping):
        User = get_saml_user_model()
        email = self.get_attribute_value('email', attributes, attribute_mapping)
        if email and create and User.objects.filter(email=email).exists():
            raise ValidationError('User with this email already exists')
        return super(WaldurSaml2Backend, self).get_saml2_user(
            create, main_attribute, attributes, attribute_mapping)
