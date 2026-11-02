from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken

from datetime import timedelta

from django.utils import timezone

from . import ratelimit
from .models import User
from .ratelimit import client_ip
from .serializers import (
    UserSerializer,
    CustomTokenObtainPairSerializer,
    ProfileUpdateSerializer,
    ChangePasswordSerializer,
)


# ============================================
# LOGIN (JWT)
# ============================================
class LoginView(TokenObtainPairView):
    """
    POST /api/auth/login/
    Body: { "email": "...", "password": "..." }
    Returns: { access, refresh, user }
    """
    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]

    # 5 wrong passwords for one email, or 30 from one IP, in 15 minutes → locked for the rest of the window.
    WINDOW = timedelta(minutes=15)
    PER_EMAIL, PER_IP = 5, 30

    def post(self, request, *args, **kwargs):
        email = str(request.data.get('email') or '').strip().lower()
        ip = client_ip(request)
        locked = ((email and ratelimit.count('login_email', email, self.WINDOW) >= self.PER_EMAIL)
                  or ratelimit.count('login_ip', ip, self.WINDOW) >= self.PER_IP)
        if locked:
            return Response({'detail': 'Too many sign-in attempts. Wait 15 minutes and try again.'},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
        from . import two_factor
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            raise InvalidToken(e.args[0])
        except AuthenticationFailed:   # wrong email or password
            if email:
                ratelimit.hit('login_email', email)
            ratelimit.hit('login_ip', ip)
            left = self.PER_EMAIL - ratelimit.count('login_email', email, self.WINDOW) if email else 0
            msg = 'Wrong email or password. Check both and try again.'
            if 0 < left <= 2:
                msg += f' {left} attempt{"s" if left != 1 else ""} left before sign-in is paused for 15 minutes.'
            elif left <= 0:
                msg = 'Too many sign-in attempts. Wait 15 minutes and try again, or reset your password.'
            return Response({'detail': msg}, status=status.HTTP_401_UNAUTHORIZED)
        if email:
            ratelimit.clear('login_email', email)
        # Password right. Accounts with 2-step verification (or roles that need it) get a ticket for
        # the second step instead of tokens; the refresh token made by the serializer is revoked.
        try:
            step = two_factor.start_login(serializer.user, request.data.get('device_token'))
        except two_factor.TwoFactorError as e:
            return Response({e.field: str(e), 'detail': str(e)}, status=e.status)
        if step:
            RefreshToken(serializer.validated_data['refresh']).blacklist()
            return Response(step)
        return Response(serializer.validated_data)


# ============================================
# REGISTER
# ============================================
@api_view(['POST'])
@permission_classes([AllowAny])
def register(request):
    """
    POST /api/auth/register/ — closed. Accounts are created when the gym approves a paid
    signup from the website (POST /api/public/register/ → admin approval).
    """
    return Response({'detail': 'Join on the Smart Gym website — your account is created when your membership is approved.'},
                    status=status.HTTP_403_FORBIDDEN)


# ============================================
# GET / UPDATE CURRENT USER
# ============================================
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def me(request):
    """
    GET  /api/auth/me/  → return current user
    PATCH /api/auth/me/ → update current user profile
    """
    if request.method == 'GET':
        return Response(UserSerializer(request.user).data)

    serializer = ProfileUpdateSerializer(
        request.user,
        data=request.data,
        partial=True,
    )
    if serializer.is_valid():
        serializer.save()
        return Response(UserSerializer(request.user).data)
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================
# CHANGE PASSWORD
# ============================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password(request):
    """
    POST /api/auth/change-password/
    Body: { old_password, new_password, new_password_confirm }
    """
    serializer = ChangePasswordSerializer(
        data=request.data,
        context={'request': request},
    )
    if serializer.is_valid():
        serializer.save()
        from .two_factor import forget_devices
        forget_devices(request.user)
        return Response({'detail': 'Password updated successfully.'})
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ============================================
# FORGOT / RESET PASSWORD
# ============================================
RESET_PER_EMAIL = 3          # reset emails per address per hour
RESET_PER_IP = 10


@api_view(['POST'])
@permission_classes([AllowAny])
def forgot_password(request):
    """
    POST /api/auth/password/forgot/  { email, admin?: bool }
    Emails a one-time reset link (valid PASSWORD_RESET_TIMEOUT, 3 days by default). The answer is
    the same whether or not the email has an account, so it can't be used to find accounts.
    """
    import logging
    from django.conf import settings
    from django.contrib.auth.tokens import default_token_generator
    from django.core.mail import send_mail
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode
    from apps.core.validators import email_error
    from apps.members.services import member_portal_url

    email = str(request.data.get('email') or '').strip().lower()
    msg = email_error(email)
    if msg:
        return Response({'email': msg}, status=status.HTTP_400_BAD_REQUEST)
    done = Response({'detail': 'If an account exists for this email, a reset link is on its way. '
                               'Check your inbox (and spam folder).'})
    ip = client_ip(request)
    window = timedelta(hours=1)
    if ratelimit.count('reset_email', email, window) >= RESET_PER_EMAIL or ratelimit.count('reset_ip', ip, window) >= RESET_PER_IP:
        return Response({'detail': 'Too many reset requests. Please try again in an hour.'},
                        status=status.HTTP_429_TOO_MANY_REQUESTS)
    ratelimit.hit('reset_email', email)
    ratelimit.hit('reset_ip', ip)

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        return done
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    staff = user.role in ('admin', 'manager')
    link = f"{member_portal_url()}/{'admin/' if staff else ''}reset-password?uid={uid}&token={token}"
    name = user.first_name or user.email
    try:
        send_mail(
            'Smart Gym — reset your password · إعادة تعيين كلمة المرور',
            f"Hi {name},\n\nSomeone (hopefully you) asked to reset your Smart Gym password.\n"
            f"Open this link to choose a new one (it works once and expires in 3 days):\n\n  {link}\n\n"
            "If you didn't ask for this, ignore this email — your password stays the same.\n— Smart Gym\n\n"
            "────────────\n\n"
            f"مرحبًا {name}،\n\nطُلب إعادة تعيين كلمة مرور حسابك في سمارت جيم.\n"
            f"افتح الرابط لاختيار كلمة مرور جديدة (يعمل مرة واحدة وينتهي خلال 3 أيام):\n\n  {link}\n\n"
            "إذا لم تطلب ذلك فتجاهل هذه الرسالة.\n— سمارت جيم",
            settings.DEFAULT_FROM_EMAIL, [user.email],
        )
    except Exception:   # noqa: BLE001
        logging.getLogger(__name__).exception('Password reset email to %s failed.', user.email)
        return Response({'detail': 'We couldn’t send the email right now. Please try again later or contact the front desk.'},
                        status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return done


@api_view(['POST'])
@permission_classes([AllowAny])
def reset_password(request):
    """
    POST /api/auth/password/reset/  { uid, token, new_password, new_password_confirm }
    """
    from django.contrib.auth.password_validation import validate_password
    from django.contrib.auth.tokens import default_token_generator
    from django.core.exceptions import ValidationError
    from django.utils.encoding import force_str
    from django.utils.http import urlsafe_base64_decode

    bad_link = Response({'detail': 'This reset link is invalid or has already been used. Ask for a new one.'},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(str(request.data.get('uid') or ''))), is_active=True)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        return bad_link
    if not default_token_generator.check_token(user, str(request.data.get('token') or '')):
        return bad_link

    new, confirm = str(request.data.get('new_password') or ''), str(request.data.get('new_password_confirm') or '')
    errors = {}
    if not new:
        errors['new_password'] = 'Enter a new password.'
    else:
        try:
            validate_password(new, user)
        except ValidationError as e:
            errors['new_password'] = ' '.join(e.messages)
    if new and new != confirm:
        errors['new_password_confirm'] = 'The two passwords don’t match.'
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new)
    user.must_change_password = False
    user.save(update_fields=['password', 'must_change_password'])
    from .two_factor import forget_devices
    forget_devices(user)
    ratelimit.clear('login_email', user.email.lower())     # a locked-out user can sign in again
    login_path = {'admin': '/admin/login', 'manager': '/admin/login', 'trainer': '/coach/login'}.get(user.role, '/login')
    return Response({'detail': 'Password changed. You can sign in now.', 'login_path': login_path})


# ============================================
# 2-STEP VERIFICATION
# ============================================
def _tokens(user, request=None):
    """
    The same body as a normal sign-in: {access, refresh, user}. With {remember_device: true} the
    browser also gets `device_token`: this device won't be asked for a code for 30 days.
    """
    from django.contrib.auth.models import update_last_login
    from . import two_factor
    refresh = CustomTokenObtainPairSerializer.get_token(user)
    update_last_login(None, user)
    body = {'refresh': str(refresh), 'access': str(refresh.access_token), 'user': UserSerializer(user).data}
    if request is not None and request.data.get('remember_device') in (True, 'true', '1', 1):
        body['device_token'] = two_factor.trust_device(user, two_factor.device_label(request))
        body['trusted_days'] = two_factor.TRUST_DAYS
    return body


def _tfa_error(e):
    return Response({e.field: str(e), 'detail': str(e)}, status=e.status)


def _need_code(request):
    if not str(request.data.get('code') or '').strip():
        return Response({'code': 'Enter the 6-digit code.'}, status=status.HTTP_400_BAD_REQUEST)
    return None


@api_view(['POST'])
@permission_classes([AllowAny])
def two_factor_login_verify(request):
    """POST /api/auth/2fa/verify/ {ticket, code} → tokens. `code` can be a backup code."""
    from . import two_factor
    missing = _need_code(request)
    if missing:
        return missing
    try:
        user = two_factor.verify_login(request.data.get('ticket'), request.data.get('code'))
    except two_factor.TwoFactorError as e:
        return _tfa_error(e)
    return Response(_tokens(user, request))


@api_view(['POST'])
@permission_classes([AllowAny])
def two_factor_resend(request):
    """POST /api/auth/2fa/resend/ {ticket} — another emailed code."""
    from . import two_factor
    try:
        return Response(two_factor.resend(request.data.get('ticket')))
    except two_factor.TwoFactorError as e:
        return _tfa_error(e)


@api_view(['POST'])
@permission_classes([AllowAny])
def two_factor_setup_method(request):
    """POST /api/auth/2fa/setup/method/ {ticket, method: totp|email} — at sign-in, for roles that need 2FA."""
    from . import two_factor
    try:
        return Response(two_factor.choose_method(request.data.get('ticket'), request.data.get('method'),
                                                 purposes=('setup',)))
    except two_factor.TwoFactorError as e:
        return _tfa_error(e)


@api_view(['POST'])
@permission_classes([AllowAny])
def two_factor_setup_confirm(request):
    """POST /api/auth/2fa/setup/confirm/ {ticket, code} → 2FA on; tokens + backup codes (shown once)."""
    from . import two_factor
    missing = _need_code(request)
    if missing:
        return missing
    try:
        user, codes = two_factor.confirm_method(request.data.get('ticket'), request.data.get('code'), purposes=('setup',))
    except two_factor.TwoFactorError as e:
        return _tfa_error(e)
    return Response({**_tokens(user, request), 'backup_codes': codes})


TFA_CHECKS_PER_15_MIN = 5


def _current_code_ok(request):
    """(ok, error response). The signed-in person's current code, with a limit on wrong tries."""
    from . import two_factor
    key = str(request.user.id)
    window = timedelta(minutes=15)
    if ratelimit.count('tfa_check', key, window) >= TFA_CHECKS_PER_15_MIN:
        return False, Response({'code': 'Too many wrong codes. Wait 15 minutes and try again.'},
                               status=status.HTTP_429_TOO_MANY_REQUESTS)
    if two_factor.check_current(request.user, request.data.get('code')):
        ratelimit.clear('tfa_check', key)
        return True, None
    ratelimit.hit('tfa_check', key)
    return False, Response({'code': 'That code isn’t right. Use the current code from your app or email, '
                                    'or one of your backup codes.'}, status=status.HTTP_400_BAD_REQUEST)


def _tfa_status(user):
    from . import two_factor
    from .models import TrustedDevice
    tf = two_factor.settings_for(user)
    devices = TrustedDevice.objects.filter(user=user, expires_at__gt=timezone.now())
    return {
        'enabled': bool(tf), 'method': tf.method if tf else None,
        'required': two_factor.is_required(user),
        'gym_enabled': two_factor.policy()['enabled'],      # off = nobody is asked for codes right now
        'trusted_devices': [{'id': d.id, 'label': d.label or 'Browser', 'created_at': d.created_at,
                             'last_used_at': d.last_used_at, 'expires_at': d.expires_at} for d in devices],
        'backup_codes_left': len(tf.backup_codes or []) if tf else 0,
        'email_available': two_factor.email_available(), 'email_hint': two_factor.email_hint(user.email),
        'confirmed_at': tf.confirmed_at if tf else None,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def two_factor_status(request):
    """GET /api/auth/2fa/ — on/off, method, whether the role requires it."""
    return Response(_tfa_status(request.user))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def two_factor_enable(request):
    """
    POST /api/auth/2fa/enable/ {password, method}  → step 1 (ticket + QR key, or a code is emailed)
    POST /api/auth/2fa/enable/ {ticket, code}      → step 2: on; returns backup codes (shown once)
    Also switches method while it's on (the password proves it's you).
    """
    from . import two_factor
    if request.data.get('ticket'):
        missing = _need_code(request)
        if missing:
            return missing
        try:
            _, codes = two_factor.confirm_method(request.data['ticket'], request.data.get('code'),
                                                 purposes=('enable',), user=request.user)
        except two_factor.TwoFactorError as e:
            return _tfa_error(e)
        return Response({**_tfa_status(request.user), 'backup_codes': codes})
    if not request.user.check_password(str(request.data.get('password') or '')):
        return Response({'password': 'Your password isn’t right.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        ticket = two_factor.start_enable(request.user)
        return Response({'ticket': ticket, **two_factor.choose_method(ticket, request.data.get('method'),
                                                                      purposes=('enable',), user=request.user)})
    except two_factor.TwoFactorError as e:
        return _tfa_error(e)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def two_factor_send_code(request):
    """POST /api/auth/2fa/send-code/ — emails a code to confirm a change (email method only)."""
    from . import two_factor
    try:
        return Response(two_factor.request_current_code(request.user))
    except two_factor.TwoFactorError as e:
        return _tfa_error(e)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def two_factor_disable(request):
    """POST /api/auth/2fa/disable/ {password, code} — members only; staff roles must keep it on."""
    from . import two_factor
    from .models import TwoFactor
    if two_factor.is_required(request.user):
        return Response({'detail': 'Your role must keep 2-step verification on. You can switch to another method instead.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if not request.user.check_password(str(request.data.get('password') or '')):
        return Response({'password': 'Your password isn’t right.'}, status=status.HTTP_400_BAD_REQUEST)
    ok, err = _current_code_ok(request)
    if not ok:
        return err
    TwoFactor.objects.filter(user=request.user).delete()
    two_factor.forget_devices(request.user)
    return Response(_tfa_status(request.user))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def two_factor_forget_devices(request):
    """POST /api/auth/2fa/devices/forget/ {id?} — one trusted browser, or all of them (no id)."""
    from . import two_factor
    from .models import TrustedDevice
    if request.data.get('id'):
        TrustedDevice.objects.filter(user=request.user, id=request.data['id']).delete()
    else:
        two_factor.forget_devices(request.user)
    return Response(_tfa_status(request.user))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def two_factor_backup_codes(request):
    """POST /api/auth/2fa/backup-codes/ {code} — a fresh set (the old ones stop working)."""
    from . import two_factor
    from .models import TwoFactor
    tf = two_factor.settings_for(request.user)
    if tf is None:
        return Response({'detail': 'Turn on 2-step verification first.'}, status=status.HTTP_400_BAD_REQUEST)
    ok, err = _current_code_ok(request)
    if not ok:
        return err
    shown, hashes = two_factor.new_backup_codes()
    TwoFactor.objects.filter(id=tf.id).update(backup_codes=hashes)
    return Response({**_tfa_status(request.user), 'backup_codes': shown})


# ============================================
# LOGOUT
# ============================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    """
    POST /api/auth/logout/
    Body: { refresh: "<refresh_token>" }
    Blacklists the refresh token.
    """
    try:
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response(
                {'detail': 'Refresh token is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        token = RefreshToken(refresh_token)
        token.blacklist()
        return Response({'detail': 'Logged out successfully.'})
    except Exception as e:
        return Response(
            {'detail': 'Invalid or expired token.'},
            status=status.HTTP_400_BAD_REQUEST,
        )