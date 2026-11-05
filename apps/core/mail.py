"""
Emails in the reader's language.

    send_localized(to, lang, bi(subject_en, subject_ar), bi(body_en, body_ar))

`lang` is 'ar' or 'en' (a user's saved preference: i18n.language(user)). The plain-text body goes out as is;
an Arabic email also carries an HTML copy laid out right to left, so mail apps align it properly.
Receipts are the exception: they stay bilingual (apps.payments.receipts).

Raises whatever the mail backend raises — callers decide whether a failed email matters.
"""
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from .i18n import Bi


def pick_lang(lang):
    return 'ar' if lang == 'ar' else 'en'


def _rtl_html(text):
    lines = '<br>'.join(escape(line) for line in text.split('\n'))
    return (f"<div dir='rtl' lang='ar' style=\"text-align:right;font-family:Tahoma,'Segoe UI',Arial,sans-serif;"
            f"font-size:15px;line-height:1.7;color:#111\">{lines}</div>")


def send_localized(to, lang, subject: Bi, body: Bi):
    lang = pick_lang(lang)
    text = body.ar if lang == 'ar' else body.en
    msg = EmailMultiAlternatives(subject.ar if lang == 'ar' else subject.en, text, settings.DEFAULT_FROM_EMAIL,
                                 [to] if isinstance(to, str) else list(to))
    if lang == 'ar':
        msg.attach_alternative(_rtl_html(text), 'text/html')
    msg.send()
