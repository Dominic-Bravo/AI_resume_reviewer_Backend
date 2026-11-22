"""
Unit tests for `api.adapters.CustomSocialAccountAdapter`.

HOW TO MAINTAIN THIS FILE WHEN ADDING FEATURES:
-------------------------------------------------------------------------------
1. Added new OAuth profile fields in `populate_user` (e.g., avatar URL,
   default free credits, or locale)?
   -> Update assertions in `test_populate_user_maps_google_extra_data` and
      `test_populate_user_uses_fallbacks_when_names_missing`.

2. Changed social account linking rules in `pre_social_login` (e.g., case-
   insensitive email lookup or verified-email checks)?
   -> Update the tests under Section 3 (Pre-Social Login Account Linking).
-------------------------------------------------------------------------------
"""

from types import SimpleNamespace

import pytest
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth import get_user_model

from api.adapters import CustomSocialAccountAdapter

User = get_user_model()


# =============================================================================
# 1. FIXTURES (Adapter Instance & Mock SocialLogin Payload)
# =============================================================================


@pytest.fixture
def adapter():
    """Returns an instance of CustomSocialAccountAdapter."""
    return CustomSocialAccountAdapter()


@pytest.fixture
def make_sociallogin(mocker):
    """Factory fixture to build a mock allauth `SocialLogin` object."""

    def _factory(extra_data=None, is_existing=False):
        account = SimpleNamespace(extra_data=extra_data or {})
        sociallogin = mocker.MagicMock()
        sociallogin.account = account
        sociallogin.is_existing = is_existing
        return sociallogin

    return _factory


# =============================================================================
# 2. POPULATE USER TESTS (Google OAuth Field Mapping & Fallbacks)
# =============================================================================


@pytest.mark.happy_path
def test_populate_user_maps_google_extra_data(mocker, adapter, make_sociallogin):
    """
    GIVEN a Google OAuth payload containing `given_name` and `family_name`
    WHEN  populate_user runs
    THEN  it assigns those values to `user.first_name` and `user.last_name`.
    """
    # Must be a real User (AbstractUser subclass) instance to pass `assert isinstance(user, AbstractUser)`
    base_user = User(email="ada@example.com")
    mocker.patch.object(
        DefaultSocialAccountAdapter, "populate_user", return_value=base_user
    )

    sociallogin = make_sociallogin(
        extra_data={"given_name": "Ada", "family_name": "Lovelace"}
    )
    populated = adapter.populate_user(request=None, sociallogin=sociallogin, data={})

    assert populated.first_name == "Ada"
    assert populated.last_name == "Lovelace"


@pytest.mark.edge_case
def test_populate_user_uses_secondary_keys_and_random_fallbacks(
    mocker, adapter, make_sociallogin
):
    """
    GIVEN a social payload missing `given_name`/`family_name` (and empty `extra_data`)
    WHEN  populate_user runs
    THEN  it falls back to `first_name`/`last_name`, and finally `"User"` + 4-digit random code.
    """
    # 1. Secondary keys (`first_name`, `last_name`)
    user_secondary = User(email="grace@example.com")
    mocker.patch.object(
        DefaultSocialAccountAdapter, "populate_user", return_value=user_secondary
    )
    sl_secondary = make_sociallogin(
        extra_data={"first_name": "Grace", "last_name": "Hopper"}
    )
    res_secondary = adapter.populate_user(request=None, sociallogin=sl_secondary, data={})
    assert res_secondary.first_name == "Grace"
    assert res_secondary.last_name == "Hopper"

    # 2. Missing all name keys -> "User" + random 4-digit fallback
    user_fallback = User(email="anon@example.com")
    mocker.patch.object(
        DefaultSocialAccountAdapter, "populate_user", return_value=user_fallback
    )
    mocker.patch("api.adapters.random.randint", return_value=4821)

    sl_empty = make_sociallogin(extra_data={})
    res_fallback = adapter.populate_user(request=None, sociallogin=sl_empty, data={})
    assert res_fallback.first_name == "User"
    assert res_fallback.last_name == "4821"


# =============================================================================
# 3. PRE-SOCIAL LOGIN TESTS (Auto-Linking Existing Email Accounts)
# =============================================================================


@pytest.mark.happy_path
def test_pre_social_login_links_existing_user_by_email(
    mocker, adapter, make_sociallogin
):
    """
    GIVEN a new social login (`is_existing=False`) whose email matches an existing User
    WHEN  pre_social_login runs
    THEN  it calls `sociallogin.connect(request, existing_user)` to link the accounts.
    """
    existing_user = SimpleNamespace(id=12, email="existing@example.com")
    get_user_mock = mocker.patch(
        "api.adapters.User.objects.get", return_value=existing_user
    )

    request = SimpleNamespace()
    sociallogin = make_sociallogin(
        extra_data={"email": "existing@example.com"}, is_existing=False
    )

    adapter.pre_social_login(request, sociallogin)

    get_user_mock.assert_called_once_with(email="existing@example.com")
    sociallogin.connect.assert_called_once_with(request, existing_user)


@pytest.mark.edge_case
def test_pre_social_login_skips_when_existing_or_no_email_or_unknown_user(
    mocker, adapter, make_sociallogin
):
    """
    GIVEN a social login that is already linked, has no email, or belongs to a brand-new user
    WHEN  pre_social_login runs
    THEN  it exits cleanly without calling `sociallogin.connect()`.
    """
    get_user_mock = mocker.patch(
        "api.adapters.User.objects.get", side_effect=User.DoesNotExist
    )

    # 1. Already linked (`is_existing=True`) -> skips DB query completely
    sl_existing = make_sociallogin(
        extra_data={"email": "a@example.com"}, is_existing=True
    )
    adapter.pre_social_login(None, sl_existing)
    get_user_mock.assert_not_called()
    sl_existing.connect.assert_not_called()

    # 2. Missing email in OAuth payload -> skips DB query
    sl_no_email = make_sociallogin(extra_data={}, is_existing=False)
    adapter.pre_social_login(None, sl_no_email)
    get_user_mock.assert_not_called()
    sl_no_email.connect.assert_not_called()

    # 3. Brand-new email (`User.DoesNotExist`) -> catches exception, does not connect
    sl_new_user = make_sociallogin(
        extra_data={"email": "brandnew@example.com"}, is_existing=False
    )
    adapter.pre_social_login(None, sl_new_user)
    get_user_mock.assert_called_once_with(email="brandnew@example.com")
    sl_new_user.connect.assert_not_called()