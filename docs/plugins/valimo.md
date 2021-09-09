# Valimo authentication plugin

Valimo endpoint allows to get Waldur authentication token using mobile
PKI from Valimo. Please note, that only authentication is supported - no
auto-registration is currently available.

- To initiate a login process, please issue POST request against
  `/api/auth-valimo/` endpoint providing phone number as an input.

- On that request Waldur will create a result object (`AuthResult`)
  and request authentication from the Valimo PKI service. The result
  object contains all the metadata about the request, including field
  `message` - text that is sent to the user via SMS. This text is
  typically shown to the user for validation purposes.

- The client is expected to poll for the authentication process by
  issuing POST requests against `/api/auth-valimo/result/` with UUID
  in the payload of a request. Please see details in the API
  documentation.

- After a successful login, endpoint `/api/auth-valimo/result/` will
  contain authentication token.
