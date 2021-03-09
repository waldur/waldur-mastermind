from waldur_client import WaldurClient


def get_client_for_offering(offering):
    options = offering.secret_options
    api_url = options['api_url']
    token = options['token']
    return WaldurClient(api_url, token)


def pull_fields(fields, local_object, remote_object):
    changed_fields = set()
    for field in fields:
        if remote_object[field] != getattr(local_object, field):
            setattr(local_object, field, remote_object[field])
            changed_fields.add(field)
    if changed_fields:
        local_object.save(update_fields=changed_fields)
