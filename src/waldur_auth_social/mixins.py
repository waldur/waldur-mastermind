import uuid

from django.contrib.auth import get_user_model

from waldur_core.core.models import SshPublicKey

User = get_user_model()


class EduteamsCreateOrUpdateUserMixin:
    def create_or_update_user(self, backend_user):
        username = backend_user.get("sub") or backend_user.get('voperson_id')
        email = backend_user.get('email')
        if backend_user.get('mail'):
            email = backend_user['mail'][0]
        first_name = backend_user['given_name']
        last_name = backend_user['family_name']
        # https://wiki.geant.org/display/eduTEAMS/Attributes+available+to+Relying+Parties#AttributesavailabletoRelyingParties-Assurance
        details = {
            'eduperson_assurance': backend_user.get('eduperson_assurance', []),
        }
        # https://wiki.geant.org/display/eduTEAMS/Attributes+available+to+Relying+Parties#AttributesavailabletoRelyingParties-AffiliationwithinHomeOrganization
        backend_affiliations = backend_user.get('voperson_external_affiliation', [])
        try:
            user = User.objects.get(username=username)
            update_fields = set()
            if user.details != details:
                user.details = details
                update_fields.add('details')
            if user.affiliations != backend_affiliations:
                user.affiliations = backend_affiliations
                update_fields.add('affiliations')
            if user.first_name != first_name:
                user.first_name = first_name
                update_fields.add('first_name')
            if user.last_name != last_name:
                user.last_name = last_name
                update_fields.add('last_name')
            if update_fields:
                user.save(update_fields=update_fields)
            created = False
        except User.DoesNotExist:
            created = True
            user = User.objects.create_user(
                username=username,
                registration_method=self.provider,
                email=email,
                first_name=first_name,
                last_name=last_name,
                details=details,
                affiliations=backend_affiliations,
            )
            user.set_unusable_password()
            user.save()

        existing_keys_map = {
            key.public_key: key
            for key in SshPublicKey.objects.filter(
                user=user, name__startswith='eduteams_'
            )
        }
        eduteams_keys = backend_user.get('ssh_public_key', [])

        new_keys = set(eduteams_keys) - set(existing_keys_map.keys())
        stale_keys = set(existing_keys_map.keys()) - set(eduteams_keys)

        for key in new_keys:
            name = 'eduteams_key_{}'.format(uuid.uuid4().hex[:10])
            new_key = SshPublicKey(user=user, name=name, public_key=key)
            new_key.save()

        for key in stale_keys:
            existing_keys_map[key].delete()

        return user, created
