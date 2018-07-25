from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from djangosaml2.conf import get_config
from djangosaml2.utils import available_idps
from saml2.attribute_converter import ac_factory
from saml2.mdstore import InMemoryMetaData, MetaDataFile, name as get_idp_name
from saml2.s_utils import UnknownSystemEntity

from . import models


def load_providers():
    metadata = {}
    for filename in settings.WALDUR_AUTH_SAML2['idp_metadata_local']:
        mdf = MetaDataFile(ac_factory(), filename)
        mdf.load()
        metadata.update(mdf.items())
    return metadata


def sync_providers():
    providers = load_providers()

    current_idps = list(models.IdentityProvider.objects.all().only('url', 'pk'))
    backend_urls = set(providers.keys())

    stale_idps = set(idp.pk for idp in current_idps if idp.url not in backend_urls)
    models.IdentityProvider.objects.filter(pk__in=stale_idps).delete()

    existing_urls = set(idp.url for idp in current_idps)

    for url, metadata in providers.items():
        name = get_idp_name(metadata)
        if not name:
            # It is expected that every provider has name. For corner cases check entity_id
            name = metadata.get('entity_id')
            if not name:
                # Skip invalid identity provider
                continue
        if url in existing_urls:
            # Skip identity provider if its url is already in the database
            continue
        models.IdentityProvider.objects.create(url=url, name=name, metadata=metadata)

    for provider in models.IdentityProvider.objects.all().iterator():
        backend_metadata = providers.get(provider.url)
        if backend_metadata and provider.metadata != backend_metadata:
            provider.metadata = backend_metadata
            provider.save()


def is_valid_idp(value):
    remote_providers = available_idps(get_config()).keys()
    return value in remote_providers or models.IdentityProvider.objects.filter(url=value).exists()


def get_idp_sso_supported_bindings(idp_entity_id, config):
    try:
        return config.metadata.service(idp_entity_id, 'idpsso_descriptor', 'single_sign_on_service').keys()
    except UnknownSystemEntity:
        return []


class DatabaseMetadataLoader(InMemoryMetaData):

    def load(self, *args, **kwargs):
        # Skip default parsing because data is not stored in file
        pass

    def __getitem__(self, item):
        try:
            return models.IdentityProvider.objects.get(url=item).metadata
        except ObjectDoesNotExist:
            raise KeyError
