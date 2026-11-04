"""
Every message the API can answer with has Arabic, and requests in Arabic get it.

The scan reads the code (not a hand-kept list): `Response({...})` values, `raise …Error('…')`,
`errors[...] = '…'` and helper `return '…'` messages. f-strings become templates with {0}, {1}.
A new English message without an entry in apps.core.messages_ar fails this test.
"""
import ast
import pathlib
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import translation
from rest_framework.test import APITestCase

from apps.core.i18n import bi, english_part, pick, translate
from apps.core.messages_ar import INTERNAL, MESSAGES

ERROR_CLASSES = re.compile(r'(Error|Exception|ValidationError)$')
ERROR_TARGETS = ('errors', 'e', 'err', 'problems')


def _template(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts, i = [], 0
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(v.value)
            else:
                parts.append('{' + str(i) + '}')
                i += 1
        return ''.join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _template(node.left), _template(node.right)
        if a is not None and b is not None:
            return a + b
    return None


def _is_message(s):
    return (bool(s) and len(s) >= 4 and ' · ' not in s and not re.search(r'[؀-ۿ]', s)
            and bool(re.match(r'^[A-Z{]', s)) and ' ' in s and not s.startswith(('GET ', 'POST ', 'PATCH ', 'http')))


def scan_messages():
    found = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self, where):
            self.where = where

        def add(self, node, at):
            t = _template(node)
            if t and _is_message(t):
                found.setdefault(t, f'{self.where}:{at}')

        def visit_Call(self, node):
            name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None) or ''
            if name == 'Response' and node.args and isinstance(node.args[0], ast.Dict):
                for v in node.args[0].values:
                    self.add(v, node.lineno)
                    if isinstance(v, (ast.List, ast.Tuple)):
                        for e in v.elts:
                            self.add(e, node.lineno)
            if ERROR_CLASSES.search(name) and node.args:
                self.add(node.args[0], node.lineno)
                if isinstance(node.args[0], ast.Dict):
                    for v in node.args[0].values:
                        self.add(v, node.lineno)
            self.generic_visit(node)

        def visit_Assign(self, node):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and getattr(target.value, 'id', '') in ERROR_TARGETS:
                    self.add(node.value, node.lineno)
            self.generic_visit(node)

        def visit_Return(self, node):
            values = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value] if node.value else []
            for v in values:
                t = _template(v)
                if t and t.endswith(('.', '!', '?')) and _is_message(t):
                    found.setdefault(t, f'{self.where}:{node.lineno}')
            self.generic_visit(node)

    root = pathlib.Path(settings.BASE_DIR) / 'apps'
    for f in root.rglob('*.py'):
        rel = f.relative_to(root.parent).as_posix()
        if '/tests' in rel or '/migrations/' in rel or '/management/' in rel:
            continue
        Visitor(rel).visit(ast.parse(f.read_text(encoding='utf-8')))
    return found


class CatalogueTests(SimpleTestCase):
    def test_every_message_has_arabic(self):
        missing = {m: where for m, where in scan_messages().items() if m not in MESSAGES and m not in INTERNAL}
        self.assertEqual(missing, {}, 'Add Arabic for these to apps/core/messages_ar.py')

    def test_arabic_uses_only_the_englishs_placeholders(self):
        # The Arabic may leave one out (an English plural "s" is {2}), but never invents one.
        for en, ar_text in MESSAGES.items():
            self.assertLessEqual(set(re.findall(r'\{\d+\}', ar_text)), set(re.findall(r'\{\d+\}', en)), en)
            if '{0}' in en:
                self.assertIn('{0}', ar_text, en)       # the main subject (a name, an amount) is always kept

    def test_templates_and_bilingual_strings(self):
        self.assertEqual(translate('Only 3 units available.'), 'المتوفر 3 وحدات فقط.')
        self.assertEqual(translate('Not enough locker credit (balance SAR 12.50).'), 'رصيد الخزانة غير كافٍ (الرصيد 12.50 ريال).')
        self.assertEqual(translate('Keep it short. · اجعله قصيرًا.'), 'اجعله قصيرًا.')
        self.assertEqual(english_part('Keep it short. · اجعله قصيرًا.'), 'Keep it short.')
        self.assertEqual(translate('Something nobody wrote yet.'), 'Something nobody wrote yet.')

    def test_pick_follows_the_request_language(self):
        msg = bi('Order paid', 'تم دفع الطلب')
        with translation.override('ar'):
            self.assertEqual(pick(msg), 'تم دفع الطلب')
        with translation.override('en'):
            self.assertEqual(pick(msg), 'Order paid')


class ApiLanguageTests(APITestCase):
    def setUp(self):
        self.member = get_user_model().objects.create_user(username='m', email='m@t.local', password='x', role='member')
        self.client.force_authenticate(self.member)

    def test_our_errors_and_drf_errors_come_in_arabic(self):
        r = self.client.post('/api/payments/checkout/', {'purpose': 'nope'}, format='json', HTTP_ACCEPT_LANGUAGE='ar')
        self.assertEqual(r.json()['detail'], 'غرض دفع غير معروف.')
        r = self.client.post('/api/payments/checkout/', {'purpose': 'nope'}, format='json', HTTP_ACCEPT_LANGUAGE='en')
        self.assertEqual(r.json()['detail'], 'Unknown payment purpose.')
        self.client.force_authenticate(None)
        r = self.client.get('/api/notifications/', HTTP_ACCEPT_LANGUAGE='ar')       # DRF's own message
        self.assertNotIn('Authentication credentials', r.json()['detail'])
        self.assertRegex(r.json()['detail'], r'[؀-ۿ]')

    def test_a_browsers_own_header_does_not_switch_language(self):
        r = self.client.post('/api/payments/checkout/', {'purpose': 'nope'}, format='json',
                             HTTP_ACCEPT_LANGUAGE='ar-SA,ar;q=0.9,en;q=0.8')
        self.assertEqual(r.json()['detail'], 'Unknown payment purpose.')

    def test_language_is_remembered_on_the_account(self):
        self.client.get('/api/notifications/', HTTP_ACCEPT_LANGUAGE='ar')
        self.member.refresh_from_db()
        self.assertEqual(self.member.language, 'ar')
        self.assertEqual(pick(bi('Hi', 'مرحبًا'), self.member), 'مرحبًا')


class NotificationLanguageTests(TestCase):
    def test_notifications_are_stored_in_both_and_read_in_the_readers(self):
        from apps.notifications.models import Notification
        from apps.notifications.service import notify
        member = get_user_model().objects.create_user(username='m', email='m@t.local', password='x', role='member')
        notify(member, 'shop', bi('Order paid', 'تم دفع الطلب'), bi('Thanks!', 'شكرًا!'))
        n = Notification.objects.get()
        self.assertEqual((n.title, n.title_ar, n.message, n.message_ar), ('Order paid', 'تم دفع الطلب', 'Thanks!', 'شكرًا!'))
        notify(member, 'system', 'Logged out successfully.', 'Retry later.')     # plain English → catalogue
        self.assertEqual(Notification.objects.latest('id').title_ar, 'تم تسجيل الخروج بنجاح.')
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(member)
        ar_rows = c.get('/api/notifications/', HTTP_ACCEPT_LANGUAGE='ar').json()
        rows = ar_rows if isinstance(ar_rows, list) else ar_rows['results']
        self.assertIn('تم دفع الطلب', [r['title'] for r in rows])
        en_rows = c.get('/api/notifications/', HTTP_ACCEPT_LANGUAGE='en').json()
        rows = en_rows if isinstance(en_rows, list) else en_rows['results']
        self.assertIn('Order paid', [r['title'] for r in rows])
