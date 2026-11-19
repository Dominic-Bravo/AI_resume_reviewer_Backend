# api/tests.py
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.settings import api_settings
from rest_framework.test import APIClient, APITestCase

from .throttles import ResumeUploadThrottle

User = get_user_model()


class ResumeUploadThrottleTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email="throttle-test@example.com",
            password="StrongPassword123!",
            credits=0,
        )
        self.client = APIClient(REMOTE_ADDR="127.0.0.1")
        self.client.force_authenticate(user=self.user)
        self.upload_url = reverse("users:upload-resume")

    def tearDown(self):
        cache.clear()
        ResumeUploadThrottle.rate = None

    def test_upload_endpoint_throttles_after_limit(self):
        ResumeUploadThrottle.rate = "2/minute"
        test_rates = {
            "anon": "30/minute",
            "user": "120/minute",
            "resume_upload": "2/minute",
        }

        with patch.object(api_settings, "DEFAULT_THROTTLE_RATES", test_rates):
            for i in range(2):
                response = self.client.post(self.upload_url, {}, format="multipart")
                self.assertNotEqual(
                    response.status_code,
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    f"Request {i + 1} should not be throttled (got {response.status_code})",
                )

            response = self.client.post(self.upload_url, {}, format="multipart")
            self.assertEqual(
                response.status_code,
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"3rd request should return 429, got {response.status_code}: {response.data}",
            )
            self.assertIn("detail", response.data)

    def test_throttling_is_isolated_per_user(self):
        ResumeUploadThrottle.rate = "1/minute"
        test_rates = {
            "anon": "30/minute",
            "user": "120/minute",
            "resume_upload": "1/minute",
        }
        other_user = User.objects.create_user(
            email="throttle-user-b@example.com",
            password="StrongPassword123!",
            credits=0,
        )
        other_client = APIClient(REMOTE_ADDR="127.0.0.1")
        other_client.force_authenticate(user=other_user)

        with patch.object(api_settings, "DEFAULT_THROTTLE_RATES", test_rates):
            # User A uses their quota and gets throttled on the 2nd request
            first = self.client.post(self.upload_url, {}, format="multipart")
            self.assertNotEqual(first.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

            throttled = self.client.post(self.upload_url, {}, format="multipart")
            self.assertEqual(throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

            # User B should still have a fresh bucket
            other_resp = other_client.post(self.upload_url, {}, format="multipart")
            self.assertNotEqual(
                other_resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS
            )


from django.core.files.uploadedfile import SimpleUploadedFile


class ResumeUploadServiceTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.upload_url = reverse("users:upload-resume")
        self.user_with_credits = User.objects.create_user(
            email="credits-yes@example.com",
            password="StrongPassword123!",
            credits=3,
        )
        self.user_no_credits = User.objects.create_user(
            email="credits-no@example.com",
            password="StrongPassword123!",
            credits=0,
        )

    def tearDown(self):
        cache.clear()
        ResumeUploadThrottle.rate = None

    def _sample_pdf(self):
        # Minimal valid PDF header/body bytes for file/hash checks
        content = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
        )
        return SimpleUploadedFile("resume.pdf", content, content_type="application/pdf")

    def test_upload_rejected_when_user_has_no_credits(self):
        self.client.force_authenticate(user=self.user_no_credits)
        response = self.client.post(
            self.upload_url,
            {"file": self._sample_pdf()},
            format="multipart",
        )
        self.assertIn(
            response.status_code,
            [
                status.HTTP_400_BAD_REQUEST,
                status.HTTP_402_PAYMENT_REQUIRED,
                status.HTTP_403_FORBIDDEN,
            ],
        )
        self.user_no_credits.refresh_from_db()
        self.assertEqual(self.user_no_credits.credits, 0)

    @patch("api.tasks.run_ai_review_task.delay")
    def test_valid_upload_deducts_credit_and_queues_task(self, mock_task_delay):
        self.client.force_authenticate(user=self.user_with_credits)
        response = self.client.post(
            self.upload_url,
            {"file": self._sample_pdf()},
            format="multipart",
        )
        self.assertIn(
            response.status_code,
            [status.HTTP_200_OK, status.HTTP_201_CREATED, status.HTTP_202_ACCEPTED],
        )
        self.user_with_credits.refresh_from_db()
        self.assertEqual(self.user_with_credits.credits, 2)
        mock_task_delay.assert_called_once()
