"""Signup photos must come from a live person: look straight, turn one way, then the other."""
import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.members import liveness
from apps.members.models import LivenessCheck, MembershipRegistration

from .faces import face_row, stub_faces
from .test_payments import PaymentTestBase, clear_image, photo


def frame_file():
    buf = io.BytesIO()
    clear_image(320).save(buf, 'JPEG', quality=70)
    return SimpleUploadedFile('frame.jpg', buf.getvalue(), content_type='image/jpeg')


class LivenessTests(PaymentTestBase):
    def start(self):
        r = self.client.post('/api/public/liveness/start/')
        self.assertEqual((r.status_code, r.data['stage']), (200, 'straight'))
        return r.data['token']

    def send(self, token, nose_shift=0.0, **row):
        with stub_faces(face_row(nose_shift=nose_shift, **row)):
            return self.client.post('/api/public/liveness/frame/', {'token': token, 'frame': frame_file()},
                                    format='multipart').data

    def passed(self):
        token = self.start()
        self.assertEqual(self.send(token)['stage'], 'turn1')
        self.assertEqual(self.send(token, 0.3)['stage'], 'turn2')
        self.assertEqual(self.send(token, 0.35)['hint_code'], 'same_side')        # same side twice doesn't count
        self.assertEqual(self.send(token, -0.3)['stage'], 'done')
        return token

    def test_a_real_head_turns_both_ways(self):
        self.passed()

    def test_a_flat_picture_never_passes(self):
        token = self.start()
        stages = {self.send(token, s)['stage'] for s in (0.02, 0.05, 0.04, 0.06, 0.03, 0.05)}
        self.assertEqual(stages, {'turn1'})      # it can face the camera, but never turns

    def test_the_face_must_stay_the_same_size(self):
        token = self.start()
        self.send(token)
        r = self.send(token, 0.3, x=10, w=290, h=300)
        self.assertEqual(r['stage'], 'turn1')
        self.assertIn('same distance', r['hint'])
        self.assertEqual(r['hint_code'], 'size')

    def test_registration_needs_the_photo_from_a_passed_check(self):
        with stub_faces(face_row()):
            self.assertEqual(self.register().status_code, 400)                       # no check at all
            token = self.start()
            r = self.client.post('/api/public/photo-check/', {'photo': photo(), 'liveness_token': token},
                                 format='multipart')
            self.assertEqual((r.status_code, r.data['code']), (400, 'not_live'))    # check not finished
        token = self.passed()
        with stub_faces(face_row()):
            r = self.client.post('/api/public/photo-check/', {'photo': photo(), 'liveness_token': token},
                                 format='multipart')
            self.assertEqual(r.data, {'ok': True, 'live': True})
            other = io.BytesIO()
            clear_image(330).save(other, 'JPEG', quality=92)
            swapped = self.register(photo=SimpleUploadedFile('x.jpg', other.getvalue(), content_type='image/jpeg'),
                                    liveness_token=token)
            self.assertIn('live face check', str(swapped.data['photo']))           # another picture
            self.assertEqual(self.register(liveness_token=token).status_code, 201)
        self.assertTrue(LivenessCheck.objects.get(stage='done').used)
        MembershipRegistration.objects.all().delete()
        with stub_faces(face_row()):
            self.assertEqual(self.register(liveness_token=token).status_code, 400)  # one signup per check

    def test_expired_or_unknown_checks(self):
        with stub_faces(face_row()):
            r = self.client.post('/api/public/liveness/frame/', {'token': 'nope', 'frame': frame_file()},
                                 format='multipart')
        self.assertEqual((r.status_code, r.data['code']), (400, 'expired'))
        with mock.patch.object(liveness, 'CHECKS_PER_IP_HOUR', 1):
            self.start()
            self.assertEqual(self.client.post('/api/public/liveness/start/').status_code, 429)

    def test_the_real_detector_sees_no_face_in_a_pattern(self):
        token = self.start()
        r = self.client.post('/api/public/liveness/frame/', {'token': token, 'frame': frame_file()}, format='multipart')
        self.assertEqual((r.data['stage'], r.data['face']), ('straight', False))
        img = Image.open(frame_file())
        self.assertEqual(img.size, (320, 320))
