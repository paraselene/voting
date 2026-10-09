from django.db import OperationalError
from django.shortcuts import render
from django.utils.cache import add_never_cache_headers
from django.utils.deprecation import MiddlewareMixin


class PrivateMiddleware(MiddlewareMixin):
    def process_exception(self, request, exception):
        if isinstance(exception, OperationalError) and 'locked' in str(exception).lower():
            return render(request, 'election/error.html', {'error': '系統忙碌，請稍候重試；未完成的交易不會更改原有選票。'}, status=503)

    def process_response(self, request, response):
        add_never_cache_headers(response)
        response['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        return response
