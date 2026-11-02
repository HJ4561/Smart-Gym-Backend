"""Signup photos must show exactly one clear, frontal face the gate can compare against."""
import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.core import face_check
from apps.core.photo_quality import photo_check
from apps.members.models import MembershipRegistration

from .faces import face_row, live_photo, stub_faces
from .test_payments import PaymentTestBase, clear_image


def jpeg(img):
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=92)
    return SimpleUploadedFile('live.jpg', buf.getvalue(), content_type='image/jpeg')


def code_for(*rows, img=None):
    with stub_faces(*rows):
        found = photo_check(jpeg(img or clear_image()))
    return found[0] if found else None


class FaceRulesTests(PaymentTestBase):
    def test_one_frontal_face_passes(self):
        self.assertIsNone(code_for(face_row()))

    def test_each_problem_has_its_own_reason(self):
        self.assertEqual(code_for(), 'no_face')
        self.assertEqual(code_for(face_row(), face_row(x=10, y=10, w=80, h=95)), 'many_faces')
        self.assertIsNone(code_for(face_row(), face_row(x=5, y=5, w=30, h=36)))    # tiny face far behind
        self.assertEqual(code_for(face_row(w=80, h=95)), 'face_small')
        self.assertEqual(code_for(face_row(x=-12)), 'face_cut')
        self.assertEqual(code_for(face_row(y=200)), 'face_cut')                    # chin below the frame
        self.assertEqual(code_for(face_row(nose_shift=0.5)), 'face_turned')
        self.assertEqual(code_for(face_row(eye_drop=35)), 'face_tilted')

    def test_face_light_and_focus_are_checked_on_the_face_itself(self):
        # a dark face against a bright background (backlit): the whole photo looks fine
        img = clear_image().copy()
        img.paste((20, 20, 20), (90, 70, 230, 240))
        self.assertEqual(code_for(face_row(), img=img), 'face_dark')
        img = clear_image().copy()
        img.paste((128, 128, 128), (90, 70, 230, 240))    # featureless face area
        self.assertEqual(code_for(face_row(), img=img), 'face_blurry')

    def test_the_real_detector_loads_and_finds_no_face_in_a_pattern(self):
        self.assertEqual(photo_check(jpeg(clear_image()))[0], 'no_face')

    def test_missing_detector_refuses_instead_of_passing(self):
        with mock.patch.object(face_check, '_get_detector', side_effect=face_check.FaceCheckUnavailable('gone')):
            self.assertEqual(photo_check(jpeg(clear_image()))[0], 'unavailable')


class PhotoCheckEndpointTests(PaymentTestBase):
    def check(self, img=None):
        return self.client.post('/api/public/photo-check/', {'photo': jpeg(img or clear_image())}, format='multipart')

    def test_answers_ok_or_the_reason(self):
        with stub_faces(face_row()):
            self.assertEqual(self.check().data, {'ok': True, 'live': False})
        with stub_faces():
            r = self.check()
        self.assertEqual((r.status_code, r.data['code']), (400, 'no_face'))
        self.assertIn('can’t find a face', r.data['photo'][0])
        r = self.check(Image.new('RGB', (320, 320), (8, 8, 8)))
        self.assertEqual((r.status_code, r.data['code']), (400, 'dark'))

    def test_missing_photo_and_unavailable_checker(self):
        r = self.client.post('/api/public/photo-check/', {}, format='multipart')
        self.assertEqual((r.status_code, r.data['code']), (400, 'missing'))
        with mock.patch.object(face_check, '_get_detector', side_effect=face_check.FaceCheckUnavailable('gone')):
            self.assertEqual(self.check().status_code, 503)

    def test_rate_limited_per_connection(self):
        from apps.public_api import views
        with mock.patch.object(views, 'PHOTO_CHECKS_PER_IP_HOUR', 2), stub_faces(face_row()):
            self.check()
            self.check()
            self.assertEqual(self.check().status_code, 429)


class RegistrationNeedsAFaceTests(PaymentTestBase):
    def test_signup_refused_without_a_face(self):
        with stub_faces():
            r = self.register()
        self.assertEqual(r.status_code, 400)
        self.assertIn('can’t find a face', str(r.data['photo']))
        self.assertFalse(MembershipRegistration.objects.exists())

    def test_signup_refused_with_two_people(self):
        with stub_faces(face_row(), face_row(x=20, w=120, h=150)):
            r = self.register()
        self.assertIn('More than one face', str(r.data['photo']))

    def test_signup_accepted_with_one_good_face(self):
        with stub_faces(face_row()), live_photo():
            self.assertEqual(self.register().status_code, 201)
