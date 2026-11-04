"""
JSON in the reader's language.

For a request in Arabic every error response (4xx/5xx) has its messages translated — `detail`,
field errors, lists of errors — and a successful response's top-level `detail` (a confirmation the
portal shows as a toast). Data is never touched: names, descriptions and the like come from the
database in both languages already. Messages written as "English · Arabic" show only the side that
matches the request, in English too.
"""
from rest_framework.renderers import JSONRenderer

from .i18n import english_part, language, translate


def _translate_tree(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_translate_tree(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _translate_tree(v, fn) for k, v in value.items()}
    return value


class BilingualJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = (renderer_context or {}).get('response')
        if response is not None and data is not None:
            fn = translate if language() == 'ar' else english_part
            if response.status_code >= 400:
                data = _translate_tree(data, fn)
            elif isinstance(data, dict) and isinstance(data.get('detail'), str):
                data = {**data, 'detail': fn(data['detail'])}
        return super().render(data, accepted_media_type, renderer_context)
