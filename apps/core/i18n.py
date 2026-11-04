"""
English and Arabic for the whole API.

Staff-entered data
    A bilingual model keeps `<field>_ar` next to `<field>`. Empty means "show the English": the portal's
    getLocalized(item, field) picks `<field>_ar` in Arabic when it has a value. `with_ar` adds the
    Arabic copies to an API row; `read_ar` takes them from a request.

Text the server writes
    `bi(en, ar)` is one message in both languages (notifications store both). `pick(text)` gives the
    one for the current request's language — or for a user's saved preference with `pick(text, user)`.
    The request language comes from the portals' `Accept-Language: ar|en` (apps.core.middleware).

Messages the API answers with
    Errors and confirmations are written in English in the code; `translate()` turns them into Arabic
    from the catalogue in apps.core.messages_ar (templates with {0}, {1} for the changing parts).
    The JSON renderer applies it to every response when the request is in Arabic.
"""
import re
from dataclasses import dataclass
from functools import lru_cache

from django.utils import translation

AR_LETTERS = re.compile(r'[؀-ۿ]')
MONTHS_AR = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو', 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر',
             'نوفمبر', 'ديسمبر']
DAYS_AR = ['الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد']


# ---------------------------------------------------------------- staff-entered data
def with_ar(obj, *fields):
    """{'name_ar': …, 'description_ar': …} for an API row."""
    return {f'{f}_ar': getattr(obj, f'{f}_ar', '') or '' for f in fields}


def read_ar(obj, data, limits, errors):
    """Copy `<field>_ar` from `data` onto `obj` (trimmed). `limits` = {field: max characters}."""
    for field, limit in limits.items():
        key = f'{field}_ar'
        if key not in data:
            continue
        value = str(data.get(key) or '').strip()
        if len(value) > limit:
            errors[key] = f'Keep it under {limit} characters. · أقل من {limit} حرفًا.'
        setattr(obj, key, value)


def ar(obj, field):
    """The Arabic of a bilingual field, or its English when there is none (for Arabic messages)."""
    return getattr(obj, f'{field}_ar', '') or getattr(obj, field, '') or ''


# ---------------------------------------------------------------- text the server writes
@dataclass(frozen=True)
class Bi:
    en: str
    ar: str

    def __str__(self):
        return self.en

    def __add__(self, other):
        other = other if isinstance(other, Bi) else Bi(str(other), str(other))
        return Bi(self.en + other.en, self.ar + other.ar)


def bi(en, ar):
    """One message in English and Arabic."""
    return Bi(str(en), str(ar))


def language(user=None):
    """'ar' or 'en': the user's saved preference if given, else the current request's language."""
    if user is not None:
        return 'ar' if getattr(user, 'language', 'en') == 'ar' else 'en'
    return 'ar' if (translation.get_language() or '').startswith('ar') else 'en'


def pick(text, user=None):
    """The text in the right language: a Bi, an "English · Arabic" string, or English (translated)."""
    lang = language(user)
    if isinstance(text, Bi):
        return text.ar if lang == 'ar' else text.en
    return translate(text) if lang == 'ar' else english_part(text)


def both(text):
    """(English, Arabic) for a Bi or a plain English message."""
    if isinstance(text, Bi):
        return text.en, text.ar
    text = str(text or '')
    return english_part(text), translate(text)


def ar_date(d, with_year=True):
    """5 نوفمبر 2026 — for Arabic messages (Latin digits, as the portal shows them)."""
    return f'{d.day} {MONTHS_AR[d.month - 1]}' + (f' {d.year}' if with_year else '')


def ar_day(d):
    """الخميس 5 نوفمبر"""
    return f'{DAYS_AR[d.weekday()]} {ar_date(d, with_year=False)}'


# ---------------------------------------------------------------- catalogue
def english_part(text):
    """"Keep it short. · اجعله قصيرًا." → "Keep it short." (other text unchanged)."""
    if isinstance(text, str) and ' · ' in text:
        left, _, right = text.partition(' · ')
        if AR_LETTERS.search(right) and not AR_LETTERS.search(left):
            return left
    return text


def _arabic_part(text):
    left, _, right = text.partition(' · ')
    if right and AR_LETTERS.search(right) and not AR_LETTERS.search(left):
        return right
    return None


@lru_cache(maxsize=1)
def _templates():
    """[(regex, arabic template)] for catalogue entries with {0}, {1}… — longest literal text first."""
    from .messages_ar import MESSAGES
    out = []
    for en, ar_text in MESSAGES.items():
        if not re.search(r'\{\d+\}', en):
            continue
        seen, parts = set(), []
        for m in re.finditer(r'\{(\d+)\}|[^{]+|\{', en):
            if m.group(1) is None:
                parts.append(re.escape(m.group(0)))
            elif m.group(1) in seen:
                parts.append(f'(?P=p{m.group(1)})')       # the same value again
            else:
                seen.add(m.group(1))
                parts.append(f'(?P<p{m.group(1)}>.+?)')
        pattern = ''.join(parts)
        literal = len(re.sub(r'\{\d+\}', '', en))
        out.append((literal, re.compile(f'^{pattern}$', re.S), ar_text))
    out.sort(key=lambda t: -t[0])
    return [(rx, ar_text) for _, rx, ar_text in out]


def translate(text):
    """English message → Arabic from the catalogue; unknown text comes back unchanged."""
    if not isinstance(text, str) or not text or AR_LETTERS.search(text) and ' · ' not in text:
        return text
    if ' · ' in text:
        found = _arabic_part(text)
        if found:
            return found
    from .messages_ar import MESSAGES
    if text in MESSAGES:
        return MESSAGES[text]
    for rx, ar_text in _templates():
        m = rx.match(text)
        if m:
            out = ar_text
            for key, value in m.groupdict().items():
                out = out.replace('{' + key[1:] + '}', translate(value) if value in MESSAGES else value)
            return out
    return text
