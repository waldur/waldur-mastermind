from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

# The pysaml2 metadata store (saml2.mdstore / attribute_converter / s_utils) and
# djangosaml2.conf transitively import `xmlschema`, a ~13 MB XSD validator, at
# module load. This module is imported at startup (serializers and views pull it),
# so the heavy symbols are imported lazily inside the functions that use them, and
# DatabaseMetadataLoader — whose base class lives in saml2.mdstore — is built on
# demand via a PEP 562 module __getattr__. See the "Lazy imports for heavy optional
# backends" section of CLAUDE.md.
from . import models


def load_providers():
    from saml2.attribute_converter import ac_factory
    from saml2.mdstore import MetaDataFile

    metadata = {}
    for filename in settings.WALDUR_AUTH_SAML2["IDP_METADATA_LOCAL"]:
        mdf = MetaDataFile(ac_factory(), filename)
        mdf.load()
        metadata.update(mdf.items())
    return metadata


def sync_providers():
    from saml2.mdstore import name as get_idp_name

    providers = load_providers()

    current_idps = list(models.IdentityProvider.objects.all().only("url", "pk"))
    backend_urls = set(providers.keys())

    stale_idps = set(idp.pk for idp in current_idps if idp.url not in backend_urls)
    models.IdentityProvider.objects.filter(pk__in=stale_idps).delete()

    existing_urls = set(idp.url for idp in current_idps)

    for url, metadata in providers.items():
        name = get_idp_name(metadata)
        if not name:
            # It is expected that every provider has name. For corner cases check entity_id
            name = metadata.get("entity_id")
            if not name:
                # Skip invalid identity provider
                continue
        if url in existing_urls:
            # Skip identity provider if its url is already in the database
            continue
        models.IdentityProvider.objects.create(url=url, name=name, metadata=metadata)

    for provider in models.IdentityProvider.objects.all():
        backend_metadata = providers.get(provider.url)
        if backend_metadata and provider.metadata != backend_metadata:
            provider.metadata = backend_metadata
            provider.save()


def is_valid_idp(value):
    from djangosaml2.conf import get_config
    from djangosaml2.utils import available_idps

    remote_providers = available_idps(get_config()).keys()
    return (
        value in remote_providers
        or models.IdentityProvider.objects.filter(url=value).exists()
    )


def get_idp_sso_supported_bindings(idp_entity_id, config):
    from saml2.s_utils import UnknownSystemEntity

    try:
        return config.metadata.service(
            idp_entity_id, "idpsso_descriptor", "single_sign_on_service"
        ).keys()
    except (UnknownSystemEntity, AttributeError):
        return []


def _build_database_metadata_loader():
    # saml2.mdstore (the InMemoryMetaData base class) pulls in xmlschema, so the
    # class is defined here rather than at module scope and materialised lazily.
    from saml2.mdstore import InMemoryMetaData

    class DatabaseMetadataLoader(InMemoryMetaData):
        def load(self, *args, **kwargs):
            # Skip default parsing because data is not stored in file
            pass

        def __getitem__(self, item):
            try:
                return models.IdentityProvider.objects.get(url=item).metadata
            except ObjectDoesNotExist:
                raise KeyError

    return DatabaseMetadataLoader


def __getattr__(name):
    # PEP 562: pysaml2 resolves the "waldur_auth_saml2.utils.DatabaseMetadataLoader"
    # string at runtime when it actually loads SAML metadata. Building the class on
    # first attribute access keeps saml2.mdstore (→ xmlschema) out of startup memory.
    if name == "DatabaseMetadataLoader":
        cls = _build_database_metadata_loader()
        globals()[name] = cls  # cache so later lookups skip __getattr__
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
