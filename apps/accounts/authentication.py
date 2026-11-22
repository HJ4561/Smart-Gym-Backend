"""
JWT authentication that enforces the "must change password" flag.

Accounts created with a temporary password (registration approval) may use
only the few endpoints needed to replace it. Enforced here, server side, so a
client that skips the member portal's redirect still can't use the API.
"""
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

# Paths (relative to /api) a flagged user may still call.
ALLOWED_WHILE_PASSWORD_CHANGE_REQUIRED = (
    '/api/auth/me/',               # the portal needs to know who is logged in (and the flag)
    '/api/auth/change-password/',
    '/api/auth/logout/',
)


class PasswordChangeRequired(PermissionDenied):
    # A dict detail is returned as-is, so clients get a stable machine-readable code:
    # 403 {"detail": "...", "code": "password_change_required"}
    default_detail = {
        'detail': 'You must change your temporary password before continuing.',
        'code': 'password_change_required',
    }
    default_code = 'password_change_required'


class EnforcingJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        # The two gyms never mix: an account only works on its own side's websites (apps.core.sides).
        from rest_framework.exceptions import AuthenticationFailed
        from apps.core.sides import set_side, side_of_request
        asked = side_of_request(request)
        if asked and asked != user.side:
            raise AuthenticationFailed('This account belongs to the other gym. Sign in on its own website.')
        set_side(user.side)          # everything in this request is the account's side
        request._request.gym_side = user.side
        if user.must_change_password and request.path not in ALLOWED_WHILE_PASSWORD_CHANGE_REQUIRED:
            raise PasswordChangeRequired()
        return user, token
