# api/tests.py
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.settings import api_settings
from rest_framework.test import APIClient, APITestCase

from .models import CreditTransaction, Resume, ScanReport
from .services import MAX_RESUME_FILE_SIZE_BYTES, ResumeService
from .tasks import run_ai_review_task
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
    @patch.object(
        ResumeService,
        "extract_resume_text",
        return_value="Experienced Python developer",
    )
    def test_valid_upload_deducts_credit_and_queues_task(
        self, _mock_extract, mock_task_delay
    ):
        self.client.force_authenticate(user=self.user_with_credits)
        with self.captureOnCommitCallbacks(execute=True):
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


class ResumeResilienceTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.upload_url = reverse("users:upload-resume")
        self.user = User.objects.create_user(
            email="resilience-a@example.com",
            password="StrongPassword123!",
            credits=5,
        )
        self.other_user = User.objects.create_user(
            email="resilience-b@example.com",
            password="StrongPassword123!",
            credits=5,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        cache.clear()
        ResumeUploadThrottle.rate = None

    def _pdf_file(self, name="resume.pdf", payload=b"%PDF-1.4\n%%EOF\n"):
        return SimpleUploadedFile(name, payload, content_type="application/pdf")

    def test_rejects_fake_pdf_signature_without_deducting_credits(self):
        fake_pdf = SimpleUploadedFile(
            "not_a_real.pdf",
            b"this is plain text pretending to be a pdf",
            content_type="application/pdf",
        )
        response = self.client.post(
            self.upload_url, {"file": fake_pdf}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 5)
        self.assertEqual(ScanReport.objects.count(), 0)

    def test_rejects_oversized_file_without_deducting_credits(self):
        oversized = SimpleUploadedFile(
            "huge.pdf",
            b"%PDF-1.4\n" + (b"x" * (MAX_RESUME_FILE_SIZE_BYTES + 1024)),
            content_type="application/pdf",
        )
        response = self.client.post(
            self.upload_url, {"file": oversized}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 5)

    @patch("api.tasks.run_ai_review_task.delay")
    @patch.object(
        ResumeService, "extract_resume_text", return_value="Python Django engineer"
    )
    def test_duplicate_inflight_scan_reuses_report_and_does_not_double_charge(
        self, _mock_extract, mock_delay
    ):
        payload = {
            "file": self._pdf_file(),
            "target_job_title": "Backend Engineer",
            "target_job_desc": "Build APIs",
        }

        with self.captureOnCommitCallbacks(execute=True):
            first_resp = self.client.post(self.upload_url, payload, format="multipart")
        self.assertEqual(first_resp.status_code, status.HTTP_201_CREATED)
        first_scan_id = first_resp.data["scan_id"]

        second_file = self._pdf_file()
        with self.captureOnCommitCallbacks(execute=True):
            second_resp = self.client.post(
                self.upload_url,
                {
                    "file": second_file,
                    "target_job_title": "  Backend Engineer  ",
                    "target_job_desc": "  Build APIs  ",
                },
                format="multipart",
            )
        self.assertEqual(second_resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_resp.data["scan_id"], first_scan_id)

        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 4)
        self.assertEqual(ScanReport.objects.count(), 1)
        mock_delay.assert_called_once()

    def test_refund_failed_scan_credit_is_idempotent(self):
        resume = Resume.objects.create(
            user=self.user,
            file_name="resume.pdf",
            file_hash="a" * 64,
            parsed_text="Sample resume text",
        )
        scan = ScanReport.objects.create(
            resume=resume,
            target_job_title="Role",
            target_job_desc="Desc",
            status=ScanReport.Status.FAILED,
        )
        self.user.credits = 4
        self.user.save(update_fields=["credits"])

        first_refund = ResumeService.refund_failed_scan_credit(scan.id)
        second_refund = ResumeService.refund_failed_scan_credit(scan.id)

        self.assertTrue(first_refund)
        self.assertFalse(second_refund)

        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 5)
        refund_txns = CreditTransaction.objects.filter(
            user=self.user, credits_added=1
        ).count()
        self.assertEqual(refund_txns, 1)

    def test_scan_detail_enforces_tenant_ownership(self):
        resume = Resume.objects.create(
            user=self.user,
            file_name="private.pdf",
            file_hash="b" * 64,
            parsed_text="Confidential resume",
        )
        scan = ScanReport.objects.create(
            resume=resume,
            target_job_title="Role",
            target_job_desc="Desc",
            status=ScanReport.Status.DONE,
            overall_score=85,
        )

        other_client = APIClient()
        other_client.force_authenticate(user=self.other_user)

        detail_url = reverse("users:scan-detail", kwargs={"scan_id": scan.id})
        resp = other_client.get(detail_url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    @patch("api.tasks.OpenAI")
    def test_run_ai_review_task_skips_already_done_scan(self, mock_openai_cls):
        resume = Resume.objects.create(
            user=self.user,
            file_name="done.pdf",
            file_hash="c" * 64,
            parsed_text="Experienced developer",
        )
        scan = ScanReport.objects.create(
            resume=resume,
            target_job_title="Role",
            target_job_desc="Desc",
            status=ScanReport.Status.DONE,
            overall_score=92,
        )

        run_ai_review_task(str(scan.id))

        mock_openai_cls.assert_not_called()
        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanReport.Status.DONE)
        self.assertEqual(scan.overall_score, 92)
