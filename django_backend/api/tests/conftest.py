import pytest


@pytest.fixture(autouse=True)
def use_in_memory_cache_for_tests(settings, mocker):
    """
    Automatically replaces Redis with Django's in-memory LocMemCache during all tests
    so DRF throttles and cache lookups run instantaneously without Docker/Redis.
    """
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "pytest-in-memory-cache",
        }
    }
    # Prevent any direct redis.Redis client calls from attempting network connections
    mocker.patch("api.views.redis_client", mocker.MagicMock())


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()