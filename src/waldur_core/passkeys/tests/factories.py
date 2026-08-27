import factory

from waldur_core.passkeys.models import PasskeyCredential
from waldur_core.passkeys.tests.helpers import RP_ID
from waldur_core.structure.tests import factories as structure_factories


class PasskeyCredentialFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PasskeyCredential

    user = factory.SubFactory(structure_factories.UserFactory)
    name = factory.Sequence(lambda n: f"Passkey {n}")
    credential_id = factory.Sequence(lambda n: f"credential-{n}")
    public_key = "cHVibGljLWtleQ"
    rp_id = RP_ID
    is_discoverable = True
    is_user_verified = True
