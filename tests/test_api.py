"""Run against the real Compose stack with RUN_INTEGRATION=1 pytest -q."""
import io
import os

import httpx
import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1', reason='Requires running Compose stack')


def test_upload_play_and_persist_guess():
    with httpx.Client(base_url=os.getenv('TEST_BASE_URL', 'http://localhost:8000'), timeout=30) as client:
        assert client.get('/api/health').json() == {'status': 'ok'}
        data = io.BytesIO()
        Image.new('RGB', (80, 60), 'blue').save(data, format='PNG')
        response = client.post('/api/challenges', data={'country': 'UA', 'explanation': 'Test photo'},
                               files={'photo': ('test.png', data.getvalue(), 'image/png')})
        assert response.status_code == 201, response.text
        response = client.post('/api/rounds')
        assert response.status_code == 201
        question = response.json()
        assert 'country' not in question and 'explanation' not in question
        assert len({option['code'] for option in question['options']}) == 4
        photo = client.get(question['image_url'])
        assert photo.status_code == 200
        assert Image.open(io.BytesIO(photo.content)).format == 'JPEG'
        path = f"/api/rounds/{question['id']}/guess"
        assert client.post(path, json={'country': 'XX'}).status_code == 422
        selected = question['options'][0]['code']
        answer = client.post(path, json={'country': selected})
        assert answer.status_code == 200
        assert answer.json()['correct'] == (selected == answer.json()['country_code'])
        assert client.post(path, json={'country': selected}).json() == answer.json()
        assert client.post(path, json={'country': question['options'][1]['code']}).status_code == 409


def test_invalid_inputs():
    with httpx.Client(base_url=os.getenv('TEST_BASE_URL', 'http://localhost:8000')) as client:
        response = client.post('/api/challenges', data={'country': 'UA'}, files={'photo': ('fake.jpg', b'not a photo', 'image/jpeg')})
        assert response.status_code == 415
        assert client.get('/api/rounds/not-a-uuid/image').status_code == 422
