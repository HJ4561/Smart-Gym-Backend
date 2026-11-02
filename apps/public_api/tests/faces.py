"""
Stand-ins for the YuNet face detector. Test photos are synthetic (no real faces), so signup
tests stub `find_faces` with rows shaped like YuNet's: x, y, w, h, then five landmarks
(right eye, left eye, nose, right & left mouth corners), then the score.
"""
from unittest.mock import patch

import numpy as np


def face_row(x=90, y=70, w=140, h=170, nose_shift=0.0, eye_drop=0.0, score=0.95):
    """A frontal face box; nose_shift turns the head (share of eye distance), eye_drop tilts it (px)."""
    rex, ley_x = x + w * 0.3, x + w * 0.7
    ey = y + h * 0.4
    eye_dist = ley_x - rex
    nose_x = (rex + ley_x) / 2 + nose_shift * eye_dist
    return np.array([x, y, w, h, rex, ey, ley_x, ey + eye_drop, nose_x, y + h * 0.6,
                     x + w * 0.35, y + h * 0.8, x + w * 0.65, y + h * 0.8, score], dtype=np.float32)


def stub_faces(*rows):
    """Patch the detector to 'find' these faces (largest first) in whatever photo it's given."""
    def fake(img):
        bgr = np.ascontiguousarray(np.asarray(img.convert('RGB'))[:, :, ::-1])
        return sorted(rows, key=lambda f: f[2] * f[3], reverse=True), bgr
    return patch('apps.core.face_check.find_faces', side_effect=fake)


class _Both:
    """Starts / stops several patchers together (setUpModule / tearDownModule)."""

    def __init__(self, *patchers):
        self.patchers = patchers

    def start(self):
        for p in self.patchers:
            p.start()

    def stop(self):
        for p in self.patchers:
            p.stop()


def live_photo():
    """Signup tests don't run the live face check (test_liveness does): any photo counts as live."""
    return patch('apps.members.liveness.registration_problem', return_value=None)


def one_face():
    return _Both(stub_faces(face_row()), live_photo())
