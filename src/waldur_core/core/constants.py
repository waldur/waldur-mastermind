from django.conf import settings

DOMAIN_MESSAGES = {
    'academic': {
        'organization owner': 'PI',
        'project manager': 'co-PI',
        'system administrator': 'member',
        'project member': 'guest',
    },
    'academic_shared': {
        'organization owner': 'resource allocator',
        'project manager': 'PI',
        'system administrator': 'co-PI',
        'project member': 'member',
    },
}


def get_domain_message(message):
    domain = settings.WALDUR_CORE['TRANSLATION_DOMAIN']
    if not domain:
        return message
    return DOMAIN_MESSAGES.get(domain, {}).get(message, message)
