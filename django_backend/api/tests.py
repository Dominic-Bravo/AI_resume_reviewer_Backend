import io
from unittest.mock import patch

import docx
import pypdf
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
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
        return_value="Experienced Python and Django engineer",
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

    def _build_real_pdf(
        self, text: str = "Senior Python Engineer with Django experience"
    ) -> SimpleUploadedFile:
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=612, height=792)

        page = writer.pages[0]
        stream_data = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        content_stream = DecodedStreamObject()
        content_stream._data = stream_data

        font_dict = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources_dict = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font_dict)}
                )
            }
        )

        page[NameObject("/Contents")] = writer._add_object(content_stream)
        page[NameObject("/Resources")] = writer._add_object(resources_dict)

        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        return SimpleUploadedFile(
            "real_resume.pdf", buf.read(), content_type="application/pdf"
        )

    def _build_real_docx(
        self, paragraphs: list[str] | None = None
    ) -> SimpleUploadedFile:
        if paragraphs is None:
            paragraphs = [
                "Backend Developer",
                "Built REST APIs with Django REST Framework, PostgreSQL, and Celery.",
            ]
        doc = docx.Document()
        for p in paragraphs:
            doc.add_paragraph(p)

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        return SimpleUploadedFile(
            "real_resume.docx",
            buf.read(),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def test_extract_resume_text_parses_real_docx(self):
        docx_file = self._build_real_docx(
            ["Backend Engineer", "Experienced in FastAPI, Django, and Redis."]
        )
        extracted = ResumeService.extract_resume_text(docx_file)
        self.assertIn("Backend Engineer", extracted)
        self.assertIn("FastAPI, Django, and Redis", extracted)

    def test_extract_resume_text_parses_real_pdf(self):
        pdf_file = self._build_real_pdf("Full Stack Developer with TypeScript")
        extracted = ResumeService.extract_resume_text(pdf_file)
        self.assertIn("Full Stack Developer", extracted)

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

    @patch("api.tasks.OpenAI")
    def test_run_ai_review_task_marks_failed_and_refunds_credit_when_retries_exhausted(
        self, mock_openai_cls
    ):
        mock_client = mock_openai_cls.return_value
        mock_client.chat.completions.create.side_effect = RuntimeError(
            "OpenRouter 503 Service Unavailable"
        )

        self.user.credits = 4
        self.user.save(update_fields=["credits"])

        resume = Resume.objects.create(
            user=self.user,
            file_name="resume.pdf",
            file_hash="d" * 64,
            parsed_text="Python developer with 3 years experience",
        )
        scan = ScanReport.objects.create(
            resume=resume,
            target_job_title="Backend Engineer",
            target_job_desc="Django + PostgreSQL",
            status=ScanReport.Status.PENDING,
        )

        with patch.object(run_ai_review_task, "max_retries", 0):
            with self.assertRaises(RuntimeError):
                run_ai_review_task.apply(args=[str(scan.id)]).get()

        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanReport.Status.FAILED)

        self.user.refresh_from_db()
        self.assertEqual(self.user.credits, 5)

        refund_count = CreditTransaction.objects.filter(
            user=self.user, credits_added=1
        ).count()
        self.assertEqual(refund_count, 1)

    @patch("api.tasks.OpenAI")
    def test_run_ai_review_task_completes_scan_on_valid_schema_response(
        self, mock_openai_cls
    ):
        valid_json = """
        {
          "overall_score": 88,
          "category_scores": [
            {"category": "backend_engineering", "score": 90},
            {"category": "impact_metrics", "score": 84}
          ],
          "missing_keywords": ["Kubernetes", "Terraform"],
          "section_critiques": [
            {
              "section": "Experience",
              "target_item": "Software Engineer",
              "critique": "Add more quantifiable latency or throughput improvements.",
              "suggested_rewrite": "Reduced API p95 response time by 35% using query optimization and Redis caching."
            }
          ]
        }
        """
        mock_choice = type(
            "Choice",
            (),
            {
                "message": type("Msg", (), {"content": valid_json})(),
                "finish_reason": "stop",
            },
        )()
        mock_usage = type(
            "Usage",
            (),
            {
                "prompt_tokens": 250,
                "completion_tokens": 180,
                "total_tokens": 430,
            },
        )()
        mock_completion = type(
            "Completion",
            (),
            {
                "choices": [mock_choice],
                "usage": mock_usage,
            },
        )()
        mock_openai_cls.return_value.chat.completions.create.return_value = (
            mock_completion
        )

        resume = Resume.objects.create(
            user=self.user,
            file_name="resume.docx",
            file_hash="e" * 64,
            parsed_text="Python and Django developer",
        )
        scan = ScanReport.objects.create(
            resume=resume,
            target_job_title="Backend Engineer",
            target_job_desc="Build resilient APIs",
            status=ScanReport.Status.PENDING,
        )

        result = run_ai_review_task.apply(args=[str(scan.id)])
        self.assertTrue(result.successful())

        scan.refresh_from_db()
        self.assertEqual(scan.status, ScanReport.Status.DONE)
        self.assertEqual(scan.overall_score, 88)
        self.assertEqual(scan.category_scores["backend_engineering"], 90)
        self.assertEqual(scan.missing_keywords, ["Kubernetes", "Terraform"])
        self.assertEqual(len(scan.section_critiques), 1)
