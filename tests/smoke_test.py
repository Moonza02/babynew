import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SMS_MODE", "disabled")

from web import app

client = app.test_client()

r = client.get('/health')
assert r.status_code == 200
assert r.get_json()['status'] == 'ok'

r = client.post('/api/auth/send-code', json={'phone': '+998901234567'})
assert r.status_code == 503
assert r.get_json()['error'] == 'sms_unavailable'

print('smoke tests: OK')
