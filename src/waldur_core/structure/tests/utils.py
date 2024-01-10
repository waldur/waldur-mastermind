def get_factory(scope):
    from waldur_core.structure.models import Customer, Project
    from waldur_core.structure.tests.factories import CustomerFactory, ProjectFactory
    from waldur_mastermind.marketplace.tests.factories import OfferingFactory

    if isinstance(scope, Project):
        return ProjectFactory
    elif isinstance(scope, Customer):
        return CustomerFactory
    else:
        return OfferingFactory


def client_list_users(client, current_user, scope):
    client.force_authenticate(user=current_user)
    url = get_factory(scope).get_url(scope) + "list_users/"
    return client.get(url)


def client_add_user(
    client, current_user, target_user, scope, role, expiration_time=None
):
    client.force_authenticate(user=current_user)
    return client.post(
        get_factory(scope).get_url(scope) + "add_user/",
        {
            "user": target_user.uuid,
            "role": role.uuid,
            "expiration_time": expiration_time,
        },
    )


def client_update_user(
    client, current_user, target_user, scope, role, expiration_time=None
):
    client.force_authenticate(user=current_user)
    return client.post(
        get_factory(scope).get_url(scope) + "update_user/",
        {
            "user": target_user.uuid,
            "role": role.uuid,
            "expiration_time": expiration_time,
        },
    )


def client_delete_user(client, current_user, target_user, scope, role):
    client.force_authenticate(user=current_user)
    return client.post(
        get_factory(scope).get_url(scope) + "delete_user/",
        {
            "user": target_user.uuid,
            "role": role.uuid,
        },
    )
