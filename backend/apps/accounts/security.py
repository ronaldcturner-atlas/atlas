import secrets
import string

from django.contrib.sessions.models import Session
from django.utils import timezone

from .models import AccountSecurityState


TEMPORARY_PASSWORD_LENGTH = 18


def generate_temporary_password():
    """Return a strong random password containing every common character class."""
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice('!@#$%^&*-_=+'),
    ]
    alphabet = string.ascii_letters + string.digits + '!@#$%^&*-_=+'
    required.extend(
        secrets.choice(alphabet)
        for _ in range(TEMPORARY_PASSWORD_LENGTH - len(required))
    )
    secrets.SystemRandom().shuffle(required)
    return ''.join(required)


def issue_temporary_password(user):
    temporary_password = generate_temporary_password()
    set_temporary_password(user, temporary_password)
    return temporary_password


def set_temporary_password(user, temporary_password):
    user.set_password(temporary_password)
    user.save(update_fields=['password'])
    state, _created = AccountSecurityState.objects.get_or_create(user=user)
    state.mark_temporary_password_issued()
    invalidate_user_sessions(user)


def user_must_change_password(user):
    if not user.is_authenticated:
        return False
    state = AccountSecurityState.objects.filter(user=user).only(
        'must_change_password',
    ).first()
    return bool(state and state.must_change_password)


def invalidate_user_sessions(user):
    """Remove every active session belonging to a user after a credential reset."""
    for session in Session.objects.filter(expire_date__gte=timezone.now()).iterator():
        try:
            session_user_id = session.get_decoded().get('_auth_user_id')
        except Exception:
            session_user_id = None
        if str(session_user_id) == str(user.pk):
            session.delete()
