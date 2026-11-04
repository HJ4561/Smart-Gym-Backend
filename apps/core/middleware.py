"""
The reader's language for each request.

The portals send `Accept-Language: ar` or `Accept-Language: en` — exactly that, nothing else. Only
those two exact values switch the language: a browser's own header ("ar-SA,ar;q=0.9,en;q=0.8") is
ignored, so other clients (the public website, scripts) keep getting English until they opt in.

With the language active, Django's and DRF's own messages come out in Arabic (their gettext
catalogues), apps.core.renderers translates ours, and a signed-in user's choice is remembered on
their account (User.language) for things sent outside a request.
"""
from django.utils import translation

LANGUAGES = ('ar', 'en')


def requested_language(request):
    value = (request.META.get('HTTP_ACCEPT_LANGUAGE') or '').strip().lower()
    return value if value in LANGUAGES else None


class LanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        lang = requested_language(request)
        translation.activate(lang or 'en')
        request.LANGUAGE_CODE = translation.get_language()
        try:
            response = self.get_response(request)
        finally:
            translation.deactivate()
        if lang:
            response.headers.setdefault('Content-Language', lang)
            self._remember(request, lang)
        return response

    @staticmethod
    def _remember(request, lang):
        # DRF puts the JWT-authenticated user on the Django request once the view has run.
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False) or getattr(user, 'language', lang) == lang:
            return
        try:
            type(user).objects.filter(pk=user.pk).update(language=lang)
        except Exception:   # noqa: BLE001 — a preference must never fail the request
            pass
