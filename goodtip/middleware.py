from django.middleware.csrf import get_token


class ForceCsrfCookieMiddleware:
    """Ensure every response carries a CSRF cookie.

    Calling ``get_token`` flags the request so ``CsrfViewMiddleware`` writes the
    ``csrftoken`` cookie on the way out. This means the cookie is present from the
    very first page a visitor hits — including the invite/join funnel — so a later
    POST (e.g. signup via an invite link) can never fail with "CSRF cookie not set".
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        get_token(request)
        return self.get_response(request)
