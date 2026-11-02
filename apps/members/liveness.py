"""
Liveness at signup: is a real person in front of the camera, or a photo of one (magazine, print,
another screen)?

The signup camera sends small frames while asking the person to (1) look straight, (2) turn their
head to one side, (3) turn it to the other side. For every frame the server measures the head
turn from the face's landmarks: how far the nose sits from the point between the eyes, as a share
of the eye distance. On a flat picture that ratio doesn't change, however the picture is tilted
or moved — only a real head turning makes it swing both ways. When both turns are seen, the
check passes; the photo taken right after is fingerprinted, and registration only accepts that
exact photo (once, within CHECK_MINUTES).

Same detector as the signup photo check and the entrance gate (apps.core.face_check).
"""
import hashlib
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from .models import LivenessCheck

STRAIGHT_MAX = 0.12      # |turn| for "looking straight"
TURN_MIN = 0.20          # |turn| for "head turned"
MAX_FRAMES = 150
CHECK_MINUTES = 15       # start → registration
STEP_SECONDS = 120       # start → both turns
SIZE_DRIFT = 0.45        # the face may not jump in size by more than this share between frames
CHECKS_PER_IP_HOUR = 30

HINTS = {
    'size': 'Keep your face at the same distance from the camera.',
    'straight_first': 'Face the camera straight on first.',
    'same_side': 'That’s the same side — turn the other way.',
}
PROMPTS = {
    'straight': 'Look straight at the camera.',
    'turn1': 'Slowly turn your head to one side.',
    'turn2': 'Now slowly turn your head to the other side.',
    'done': 'Done — look straight at the camera for your photo.',
}


class LivenessError(Exception):
    def __init__(self, message, code='not_live', status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def _hash(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def start(ip=''):
    """A new check. Returns (token, first prompt)."""
    from apps.accounts import ratelimit
    if ratelimit.count('liveness_ip', ip, timedelta(hours=1)) >= CHECKS_PER_IP_HOUR:
        raise LivenessError('Too many face checks from this connection. Try again in an hour.', 'rate', 429)
    ratelimit.hit('liveness_ip', ip)
    token = secrets.token_urlsafe(32)
    LivenessCheck.objects.create(token_hash=_hash(token), ip=ip[:64])
    if LivenessCheck.objects.count() % 50 == 0:
        LivenessCheck.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()
    return token, {'stage': 'straight', 'prompt': PROMPTS['straight']}


def _get(token, lock=True):
    qs = LivenessCheck.objects.select_for_update() if lock else LivenessCheck.objects
    c = qs.filter(token_hash=_hash(str(token or ''))).first()
    if c is None or c.used or c.created_at < timezone.now() - timedelta(minutes=CHECK_MINUTES):
        raise LivenessError('The face check expired. Start it again.', 'expired')
    return c


def head_turn(img):
    """(turn ratio, face width) of the one face in a Pillow image. Raises LivenessError."""
    from apps.core.face_check import FaceCheckUnavailable, find_faces
    try:
        faces, _ = find_faces(img)
    except (FaceCheckUnavailable, ImportError):
        raise LivenessError('We can’t check faces right now. Please try again in a few minutes.', 'unavailable', 503)
    if not faces:
        raise LivenessError('We can’t see your face — keep it inside the oval.', 'no_face')
    main = faces[0]
    if any(f[2] * f[3] >= 0.25 * main[2] * main[3] for f in faces[1:]):
        raise LivenessError('Only you should be in front of the camera.', 'many_faces')
    rex, _, lex, _, nx = (float(v) for v in main[4:9])
    eye_dist = abs(lex - rex)
    if eye_dist < 1:
        raise LivenessError('We can’t see your face — keep it inside the oval.', 'no_face')
    return (nx - (rex + lex) / 2) / eye_dist, float(main[2])


def _open(file):
    try:
        file.seek(0)
        img = Image.open(file)
        img.load()
        return ImageOps.exif_transpose(img).convert('RGB')
    except (UnidentifiedImageError, OSError, ValueError):
        raise LivenessError('That camera frame couldn’t be read.', 'unreadable')


def frame(token, file):
    """One camera frame. Moves the check along when the head does what was asked."""
    img = _open(file)
    with transaction.atomic():
        c = _get(token)
        if c.stage == 'done':
            return {'stage': 'done', 'prompt': PROMPTS['done']}
        if c.frames >= MAX_FRAMES or c.created_at < timezone.now() - timedelta(seconds=STEP_SECONDS):
            raise LivenessError('The face check took too long. Start it again.', 'expired')
        c.frames += 1
        hint = ''
        try:
            turn, width = head_turn(img)
        except LivenessError as e:
            if e.status >= 500:
                raise
            c.save(update_fields=['frames'])
            return {'stage': c.stage, 'prompt': PROMPTS[c.stage], 'face': False, 'hint': str(e), 'hint_code': e.code}
        if c.face_width and abs(width - c.face_width) > SIZE_DRIFT * c.face_width:
            hint = 'size'
        elif c.stage == 'straight':
            if abs(turn) <= STRAIGHT_MAX:
                c.stage, c.face_width = 'turn1', width
            else:
                hint = 'straight_first'
        elif c.stage == 'turn1':
            if abs(turn) >= TURN_MIN:
                c.stage, c.first_turn = 'turn2', 1 if turn > 0 else -1
        elif c.stage == 'turn2':
            if abs(turn) >= TURN_MIN and (1 if turn > 0 else -1) == -c.first_turn:
                c.stage = 'done'
            elif abs(turn) >= TURN_MIN:
                hint = 'same_side'
        c.save(update_fields=['frames', 'stage', 'first_turn', 'face_width'])
        return {'stage': c.stage, 'prompt': PROMPTS[c.stage], 'face': True, 'hint': HINTS.get(hint, ''), 'hint_code': hint}


def attach_photo(token, file):
    """After a passed check: the photo that may be used to register (its fingerprint is kept)."""
    with transaction.atomic():
        c = _get(token)
        if c.stage != 'done':
            raise LivenessError('Finish the face check first: look straight, then turn your head to each side.')
        file.seek(0)
        c.photo_sha256 = _hash(file.read())
        file.seek(0)
        c.save(update_fields=['photo_sha256'])


def registration_problem(token, file):
    """Error message, or None when this exact photo was taken after a passed check."""
    try:
        c = _get(token, lock=False)
    except LivenessError:
        return 'The live face check expired or is missing. Open the camera and take your photo again.'
    file.seek(0)
    digest = _hash(file.read())
    file.seek(0)
    if c.stage != 'done' or not c.photo_sha256 or c.photo_sha256 != digest:
        return 'Take your photo with the live face check on this page (a picture of a photo isn’t accepted).'
    return None


def mark_used(token):
    """One registration per check."""
    LivenessCheck.objects.filter(token_hash=_hash(str(token or ''))).update(used=True)
