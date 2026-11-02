"""Public-site content (texts + images), contact inbox, and seeding."""
import io
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APITestCase

from apps.site_content.models import ContactMessage, SiteImage, SiteText
from apps.site_content.seeding import seed
from .faces import one_face

# Test photos have no real face — stub the detector to find one well-placed face.
_faces = one_face()


def setUpModule():
    _faces.start()


def tearDownModule():
    _faces.stop()

User = get_user_model()
ADMIN = '/api/admin/site-content'


def png(size=(8, 8)):
    buf = io.BytesIO()
    Image.new('RGB', size, 'red').save(buf, 'PNG')
    return SimpleUploadedFile('hero.png', buf.getvalue(), content_type='image/png')


class ContentTestBase(APITestCase):
    def setUp(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        o = override_settings(MEDIA_ROOT=media)
        o.enable()
        self.addCleanup(o.disable)
        cache.clear()   # throttle counters
        self.admin = User.objects.create_user(username='a', email='a@t.local', password='x', role='admin')

    def as_admin(self):
        self.client.force_authenticate(self.admin)


class SeededContentTests(ContentTestBase):
    def test_migration_seeds_every_text_and_image(self):
        self.assertEqual(SiteText.objects.count(), 874)
        self.assertEqual(SiteImage.objects.count(), 25)
        self.assertFalse(SiteText.objects.filter(page='bkm').exists())      # booking removed
        self.assertFalse(SiteImage.objects.filter(key__startswith='home.sectors.').exists())  # home uses the facilities list
        self.assertTrue(SiteText.objects.filter(key='ct.phone').exists())                     # added in 0005
        self.assertFalse(SiteText.objects.filter(key__startswith='fac.c1.').exists())   # now a facilities item
        self.assertFalse(SiteImage.objects.filter(key='ins.vo2').exists())              # now an article image
        nav = SiteText.objects.get(key='nav.home')
        self.assertTrue(nav.en and nav.ar)
        self.assertFalse(nav.is_customized)

    def test_public_endpoint_returns_all_texts_and_images(self):
        r = self.client.get('/api/public/site-content/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["texts"]), 874)
        self.assertEqual(set(r.data['texts']['nav.home']), {'en', 'ar'})
        self.assertTrue(r.data['images']['fac.heroBg']['url'].startswith('https://'))
        self.assertIn('max-age=60', r['Cache-Control'])


class TextEditingTests(ContentTestBase):
    def test_admin_edits_text_and_public_sees_it(self):
        self.as_admin()
        r = self.client.patch(f'{ADMIN}/texts/nav.home/', {'en': 'Start', 'ar': 'البداية'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['is_customized'])
        self.assertEqual(r.data['updated_by_email'], 'a@t.local')
        self.client.force_authenticate(None)
        public = self.client.get('/api/public/site-content/').data['texts']['nav.home']
        self.assertEqual(public, {'en': 'Start', 'ar': 'البداية'})

    def test_key_and_defaults_are_read_only(self):
        self.as_admin()
        before = SiteText.objects.get(key='nav.home')
        self.client.patch(f'{ADMIN}/texts/nav.home/', {'key': 'x', 'default_en': 'x', 'page': 'x'}, format='json')
        after = SiteText.objects.get(key='nav.home')
        self.assertEqual((after.key, after.page, after.default_en), (before.key, before.page, before.default_en))

    def test_reset_restores_shipped_text(self):
        self.as_admin()
        default = SiteText.objects.get(key='nav.home').default_en
        self.client.patch(f'{ADMIN}/texts/nav.home/', {'en': 'Changed'}, format='json')
        r = self.client.post(f'{ADMIN}/texts/nav.home/reset/')
        self.assertEqual(r.data['en'], default)
        self.assertFalse(r.data['is_customized'])

    def test_filters_by_page_search_and_customized(self):
        self.as_admin()
        self.assertTrue(all(t['page'] == 'foot' for t in self.client.get(f'{ADMIN}/texts/', {'page': 'foot'}).data))
        self.client.patch(f'{ADMIN}/texts/nav.home/', {'en': 'Zebra'}, format='json')
        self.assertEqual([t['key'] for t in self.client.get(f'{ADMIN}/texts/', {'customized': 'true'}).data], ['nav.home'])
        self.assertIn('nav.home', [t['key'] for t in self.client.get(f'{ADMIN}/texts/', {'q': 'zebra'}).data])

    def test_pages_overview_counts(self):
        self.as_admin()
        self.client.patch(f'{ADMIN}/texts/nav.home/', {'en': 'Changed'}, format='json')
        pages = {p['page']: p for p in self.client.get(f'{ADMIN}/pages/').data}
        self.assertEqual(pages['nav']['texts_customized'], 1)
        self.assertEqual(pages['fac']['images'], 1)       # hero; card images moved to the facilities list
        self.assertEqual(sum(p['texts'] for p in pages.values()), 874)   # every page is listed

    def test_unknown_key_is_404(self):
        self.as_admin()
        self.assertEqual(self.client.patch(f'{ADMIN}/texts/nope.key/', {'en': 'x'}, format='json').status_code, 404)


class ImageTests(ContentTestBase):
    def test_upload_replaces_the_public_image_and_reset_restores_it(self):
        self.as_admin()
        default = SiteImage.objects.get(key='fac.heroBg').default_url
        r = self.client.post(f'{ADMIN}/images/fac.heroBg/', {'image': png(), 'alt_en': 'Pool'}, format='multipart')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['is_customized'])
        self.assertNotEqual(r.data['url'], default)
        self.assertEqual(r.data['alt_en'], 'Pool')
        public = self.client.get('/api/public/site-content/').data['images']['fac.heroBg']
        self.assertEqual(public['url'], r.data['url'])
        img = SiteImage.objects.get(key='fac.heroBg')
        storage, name = img.image.storage, img.image.name
        r = self.client.post(f'{ADMIN}/images/fac.heroBg/reset/')
        self.assertEqual(r.data['url'], default)
        self.assertFalse(storage.exists(name))     # uploaded file removed

    def test_replacing_deletes_the_previous_upload(self):
        self.as_admin()
        self.client.post(f'{ADMIN}/images/fac.heroBg/', {'image': png()}, format='multipart')
        first = SiteImage.objects.get(key='fac.heroBg').image
        storage, first_name = first.storage, first.name
        self.client.post(f'{ADMIN}/images/fac.heroBg/', {'image': png((16, 16))}, format='multipart')
        self.assertFalse(storage.exists(first_name))

    def test_alt_text_only(self):
        self.as_admin()
        r = self.client.patch(f'{ADMIN}/images/fac.heroBg/', {'alt_ar': 'مسبح'}, format='json')
        self.assertEqual(r.data['alt_ar'], 'مسبح')
        self.assertFalse(r.data['is_customized'])

    def test_rejects_non_images_and_missing_file(self):
        self.as_admin()
        txt = SimpleUploadedFile('x.txt', b'hello', content_type='text/plain')
        self.assertEqual(self.client.post(f'{ADMIN}/images/fac.heroBg/', {'image': txt}, format='multipart').status_code, 400)
        self.assertEqual(self.client.post(f'{ADMIN}/images/fac.heroBg/', {}, format='multipart').status_code, 400)


class PermissionTests(ContentTestBase):
    def test_only_admin_or_manager(self):
        member = User.objects.create_user(username='m', email='m@t.local', password='x')
        for user, expected in ((None, 401), (member, 403)):
            self.client.force_authenticate(user)
            with self.subTest(user=user):
                self.assertEqual(self.client.get(f'{ADMIN}/texts/').status_code, expected)
                self.assertEqual(self.client.patch(f'{ADMIN}/texts/nav.home/', {'en': 'x'}, format='json').status_code, expected)
                self.assertEqual(self.client.get(f'{ADMIN}/messages/').status_code, expected)
        manager = User.objects.create_user(username='g', email='g@t.local', password='x', role='manager')
        self.client.force_authenticate(manager)
        self.assertEqual(self.client.get(f'{ADMIN}/texts/').status_code, 200)


class ContactTests(ContentTestBase):
    def send(self, **over):
        body = {'name': 'Sara', 'email': 'sara@example.com', 'topic': 'membership', 'message': 'How do I freeze?', **over}
        return self.client.post('/api/public/contact/', body, format='json')

    def test_message_lands_in_the_admin_inbox(self):
        self.assertEqual(self.send().status_code, 201)
        self.as_admin()
        data = self.client.get(f'{ADMIN}/messages/').data
        self.assertEqual(data['counts'], {'new': 1})
        msg_id = data['results'][0]['id']
        self.assertEqual(self.client.patch(f'{ADMIN}/messages/{msg_id}/', {'status': 'read'}, format='json').data['status'], 'read')
        self.assertEqual(self.client.delete(f'{ADMIN}/messages/{msg_id}/').status_code, 204)

    def test_validation(self):
        self.assertEqual(self.send(email='nope').status_code, 400)
        self.assertEqual(self.send(message='hi').status_code, 400)

    def test_honeypot_is_silently_dropped(self):
        self.assertEqual(self.send(website='http://spam').status_code, 201)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_throttled_after_five_per_hour(self):
        codes = [self.send().status_code for _ in range(6)]
        self.assertEqual(codes, [201] * 5 + [429])

    @override_settings(CONTACT_NOTIFY_EMAIL='staff@gym.local')
    def test_staff_get_an_email(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.send()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['staff@gym.local'])
        self.assertIn('How do I freeze?', mail.outbox[0].body)


class SeedingTests(TestCase):
    def test_reseed_keeps_admin_edits_and_refreshes_untouched_defaults(self):
        edited = SiteText.objects.get(key='nav.home')
        edited.en = 'Admin edit'
        edited.save()
        untouched = SiteText.objects.get(key='nav.facilities')
        SiteText.objects.filter(pk=edited.pk).update(default_en='old shipped')
        SiteText.objects.filter(pk=untouched.pk).update(default_en='old shipped', en='old shipped')
        SiteText.objects.filter(key='foot.rights').delete()
        stats = seed(SiteText, SiteImage)
        self.assertEqual(SiteText.objects.get(key='nav.home').en, 'Admin edit')            # kept
        self.assertNotEqual(SiteText.objects.get(key='nav.home').default_en, 'old shipped')
        self.assertNotEqual(SiteText.objects.get(key='nav.facilities').en, 'old shipped')  # followed default
        self.assertTrue(SiteText.objects.filter(key='foot.rights').exists())               # re-added
        self.assertEqual(stats['texts_added'], 1)

    def test_reseed_is_idempotent(self):
        self.assertEqual(seed(SiteText, SiteImage),
                         {'texts_added': 0, 'texts_refreshed': 0, 'images_added': 0, 'images_refreshed': 0})
