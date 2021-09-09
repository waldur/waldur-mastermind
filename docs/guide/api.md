# REST API

## Authentication

Waldur uses token-based authentication for REST.

In order to authenticate your requests first obtain token from any of
the supported token backends. Then use the token in all the subsequent
requests putting it into `Authorization` header:

``` http
GET /api/projects/ HTTP/1.1
Accept: application/json
Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
Host: example.com
```

Also token can be put as request GET parameter, with key `x-auth-token`:

``` http
GET /api/?x-auth-token=Token%20144325be6f45e1cb1a4e2016c4673edaa44fe986 HTTP/1.1
Accept: application/json
Host: example.com
```

## API version

In order to retrieve current version of the Waldur authenticated user
should send a GET request to **/api/version/**.

Valid request example (token is user specific):

``` http
GET /api/version/ HTTP/1.1
Content-Type: application/json
Accept: application/json
Authorization: Token c84d653b9ec92c6cbac41c706593e66f567a7fa4
Host: example.com
```

Valid response example:

``` http
HTTP/1.0 200 OK
Content-Type: application/json
Vary: Accept
Allow: OPTIONS, GET

{
    "version": "0.3.0"
}
```

## Pagination

Every Waldur REST request supports pagination. Links to the next,
previous, first and last pages are included in the Link header.
*X-Result-Count* contains a count of all entries in the response set.

By default page size is set to 10. Page size can be modified by passing
**?page_size=N** query parameter. The maximum page size is 100.

Example of the header output for user listing:

``` http
HTTP/1.0 200 OK
Vary: Accept
Content-Type: application/json
Link:
 <http://example.com/api/users/?page=1>; rel="first",
 <http://example.com/api/users/?page=3>; rel="next",
 <http://example.com/api/users/?page=1>; rel="prev",
 <http://example.com/api/users/?page=6>; rel="last"
X-Result-Count: 54
Allow: GET, POST, HEAD, OPTIONS
```
