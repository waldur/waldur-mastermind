# Valimo Authentication Plugin

The Valimo plugin enables Waldur authentication using mobile PKI (Public Key Infrastructure) from Valimo.

**Note:** Only authentication is supported - auto-registration is not available.

## Authentication Flow

### 1. Initiate Login

Issue a POST request to `/api/auth-valimo/` with the user's phone number:

```json
{
  "phone": "1234567890"
}
```

Waldur will create an `AuthResult` object and request authentication from the Valimo PKI service. The response includes a `message` field containing the verification code sent to the user via SMS.

### 2. Poll for Result

Poll the authentication status by issuing POST requests to `/api/auth-valimo/result/` with the UUID from step 1:

```json
{
  "uuid": "e42473f39c844333a80107e139a4dd06"
}
```

### 3. Check Authentication State

The response will contain one of the following states:

| State | Description |
|-------|-------------|
| `Scheduled` | Login process is scheduled |
| `Processing` | Login is in progress |
| `OK` | Login was successful. Response will contain token. |
| `Canceled` | Login was canceled by user or timed out. Check `details` field for more info. |
| `Erred` | Unexpected exception during login process |

### 4. Retrieve Token

After successful login (`state: OK`), the `/api/auth-valimo/result/` response will contain the authentication token.

## Configuration

The Valimo authentication method must be enabled in Waldur settings. See the [Configuration Guide](../../admin-guide/mastermind-configuration/configuration-guide.md) for details on enabling authentication methods.
