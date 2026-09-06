import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import OTPPurpose, OTPVerification


OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 5
OTP_MAX_ATTEMPTS = 5


def hash_otp(code):
    """
    Hash an OTP before storing it in the database.
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def generate_otp():
    """
    Generate a secure 6-digit OTP.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


def create_otp(destination, purpose, user=None):
    """
    Create a new OTP verification record.

    In development, the generated OTP is returned so it can
    be displayed/logged by the development authentication flow.

    In production, the OTP should be delivered through an
    SMS/email provider instead.
    """

    # Invalidate previous unused OTPs for the same destination
    # and purpose.
    OTPVerification.objects.filter(
        destination=destination,
        purpose=purpose,
        is_used=False,
    ).update(is_used=True)

    code = generate_otp()

    otp = OTPVerification.objects.create(
        user=user,
        destination=destination,
        purpose=purpose,
        code_hash=hash_otp(code),
        expires_at=timezone.now()
        + timedelta(minutes=OTP_EXPIRY_MINUTES),
        max_attempts=OTP_MAX_ATTEMPTS,
    )

    return otp, code


def verify_otp(destination, purpose, code):
    """
    Verify an OTP.

    Returns:
        (True, otp)  -> successful verification
        (False, otp) -> failed verification
        (False, None) -> no valid OTP found
    """

    otp = (
        OTPVerification.objects
        .filter(
            destination=destination,
            purpose=purpose,
            is_used=False,
        )
        .order_by("-created_at")
        .first()
    )

    if not otp:
        return False, None

    # Expired OTP.
    if otp.is_expired:
        return False, otp

    # Too many attempts.
    if otp.attempts >= otp.max_attempts:
        return False, otp

    # Count the attempt.
    otp.attempts += 1

    if hash_otp(code) != otp.code_hash:
        otp.save(update_fields=["attempts"])
        return False, otp

    # Successful verification.
    otp.is_used = True
    otp.save(update_fields=["attempts", "is_used"])

    return True, otp


def is_development():
    """
    Determine whether Pheral is running in development mode.
    """
    return settings.DEBUG




def send_otp(destination, purpose, user=None):
    """
    Development-friendly OTP delivery.

    DEBUG=True:
        No SMS provider is required.
        The OTP is returned directly.

    DEBUG=False:
        This is where the real SMS/email provider will
        eventually be connected.
    """

    otp, code = create_otp(
        destination=destination,
        purpose=purpose,
        user=user,
    )

    if settings.DEBUG:
        return {
            "success": True,
            "otp": code,
            "verification_id": otp.id,
            "development": True,
        }

    # Production provider will be connected here later.
    return {
        "success": False,
        "otp": None,
        "verification_id": otp.id,
        "development": False,
        "message": "OTP delivery provider is not configured.",
    }