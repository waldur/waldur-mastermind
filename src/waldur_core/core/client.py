from waldur_api_client.client import AuthenticatedClient


def get_waldur_client(api_url, token):
    return AuthenticatedClient(
        base_url=api_url.rstrip("/api"),
        token=token,
        prefix="Token",
        raise_on_unexpected_status=True,
    )
