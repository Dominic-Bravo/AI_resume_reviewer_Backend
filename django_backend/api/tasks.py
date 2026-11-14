import json
import urllib.request

from celery import shared_task


@shared_task
def test_full_connection():
    url = "http://fastapi:8001/"
    with urllib.request.urlopen(url) as response:
        data = json.loads(response.read().decode())
    return {"celery_status": "Connected!", "fastapi_reply": data}
