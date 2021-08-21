from django.conf import settings

DOMAIN_MESSAGES = {
    'academic': {
        'organization owner': 'PI',
        'project manager': 'co-PI',
        'system administrator': 'member',
        'project member': 'guest',
    }
}


def get_domain_message(message):
    domain = settings.WALDUR_CORE['TRANSLATION_DOMAIN']
    if not domain:
        return message
    return DOMAIN_MESSAGES.get(domain, {}).get(message, message)
