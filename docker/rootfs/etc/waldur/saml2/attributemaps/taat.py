from saml2.attributemaps.basic import MAP as __BASIC_MAP__
from saml2.attributemaps.saml_uri import MAP as __URI_MAP__

# XXX: TAAT returns attributes with format urn:oasis:names:tc:SAML:2.0:attrname-format:basic
# as urn:oasis:names:tc:SAML:2.0:attrname-format:uri,
# so we have to extend this MAP with attribute names from basic.py

BASIC_MAP = __BASIC_MAP__

URI_MAP = {**__URI_MAP__, 'fro': {**__URI_MAP__['fro'], **__BASIC_MAP__['fro']}}
