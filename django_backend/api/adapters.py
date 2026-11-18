# api/adapters.py
import random

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth import get_user_model

User = get_user_model()


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        """
        Populates your custom User model fields using data returned
        from the Google OAuth payload.
        """
        user = super().populate_user(request, sociallogin, data)
        extra_data = sociallogin.account.extra_data

        # Extract first name and last name with sensible fallbacks
        user.first_name = (
            extra_data.get("given_name") or extra_data.get("first_name") or "User"
        )
        user.last_name = (
            extra_data.get("family_name")
            or extra_data.get("last_name")
            or str(random.randint(1000, 9999))
        )

        return user

    def pre_social_login(self, request, sociallogin):
        """
        Automatically links the Google social account to an existing standard
        email account if the email addresses match, preventing duplicate user accounts.
        """
        if sociallogin.is_existing:
            return

        email = sociallogin.account.extra_data.get("email")
        if not email:
            return

        try:
            user = User.objects.get(email=email)
            sociallogin.connect(request, user)
        except User.DoesNotExist:
            pass
