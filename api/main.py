import io
import logging
import os
import random
import time
import warnings
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import certifi
import pycountry
from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential, WorkloadIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
COUNTRIES = {c.alpha_2: c.name for c in pycountry.countries}
MAX_UPLOAD = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000
log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def azure_credential():
    # Be explicit in AKS; do not fall back to the node identity on federation failure.
    if os.getenv('AZURE_FEDERATED_TOKEN_FILE'):
        return WorkloadIdentityCredential()
    return DefaultAzureCredential()


def db():
    options = {}
    if os.getenv('POSTGRES_AUTH') == 'entra':
        # Acquire for each new connection; the credential caches and refreshes tokens.
        options['password'] = azure_credential().get_token(
            'https://ossrdbms-aad.database.windows.net/.default').token
        options['sslmode'] = 'verify-full'
        options['sslrootcert'] = certifi.where()
    return psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, **options)


def storage():
    if os.getenv('AZURE_STORAGE_ACCOUNT_URL'):
        client = BlobServiceClient(os.environ['AZURE_STORAGE_ACCOUNT_URL'],
                                   credential=azure_credential(), api_version='2023-11-03')
    else:
        client = BlobServiceClient.from_connection_string(
            os.environ['AZURE_STORAGE_CONNECTION_STRING'], api_version='2023-11-03')
    return client.get_container_client(os.getenv('AZURE_STORAGE_CONTAINER', 'challenges'))


def initialize():
    with db() as conn:
        # Serialize first-time schema creation across Kubernetes replicas.
        conn.execute('SELECT pg_advisory_xact_lock(724031581)')
        conn.execute('''CREATE TABLE IF NOT EXISTS challenges (
            id UUID PRIMARY KEY, object_key TEXT NOT NULL, country CHAR(2) NOT NULL,
            explanation TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS rounds (
            id UUID PRIMARY KEY, challenge_id UUID NOT NULL REFERENCES challenges(id),
            options JSONB NOT NULL, guess CHAR(2), correct BOOLEAN,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), answered_at TIMESTAMPTZ
        )''')
    with storage() as container:
        if os.getenv('AZURE_STORAGE_ACCOUNT_URL'):
            # Cloud containers are provisioned by Pulumi; app access is container-scoped.
            container.get_container_properties()
        else:
            try:
                container.create_container()
            except ResourceExistsError:
                pass


@asynccontextmanager
async def lifespan(app):
    for attempt in range(15):
        try:
            initialize()
            break
        except Exception:
            if attempt == 14:
                raise
            log.warning('Waiting for database and blob storage', exc_info=True)
            time.sleep(2)
    yield


app = FastAPI(title='Demo App', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=ROOT / 'web'), name='static')


@app.get('/')
def home():
    return FileResponse(ROOT / 'web' / 'index.html')


@app.get('/api/health')
def health():
    with db() as conn:
        conn.execute('SELECT 1')
    with storage() as container:
        container.get_container_properties()
    return {'status': 'ok'}


@app.get('/api/countries')
def countries():
    return [{'code': code, 'name': name} for code, name in sorted(COUNTRIES.items(), key=lambda c: c[1])]


def normalize_image(data):
    if not data or len(data) > MAX_UPLOAD:
        raise HTTPException(413, 'Choose an image up to 8 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in {'JPEG', 'PNG', 'WEBP'}:
                    raise HTTPException(415, 'Use a JPEG, PNG, or WebP photo.')
                original.load()
                picture = ImageOps.exif_transpose(original).convert('RGB')
                picture.thumbnail((2400, 2400))
                # Copy pixels into a fresh image to discard EXIF, GPS, and comments.
                clean = Image.new('RGB', picture.size)
                clean.paste(picture)
                output = io.BytesIO()
                clean.save(output, format='JPEG', quality=88)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, 'That file is not a supported image, or its dimensions are too large.')


@app.post('/api/challenges', status_code=201)
def upload_challenge(photo: UploadFile = File(...), country: str = Form(...), explanation: str = Form('')):
    if country not in COUNTRIES:
        raise HTTPException(422, 'Choose a valid country.')
    explanation = explanation.strip()
    if len(explanation) > 500:
        raise HTTPException(422, 'Keep the explanation under 500 characters.')
    data = normalize_image(photo.file.read(MAX_UPLOAD + 1))
    challenge_id = uuid4()
    key = f'{challenge_id}.jpg'
    with storage() as container:
        container.upload_blob(key, data, content_settings=ContentSettings(content_type='image/jpeg'))
        try:
            with db() as conn:
                conn.execute('INSERT INTO challenges (id, object_key, country, explanation) VALUES (%s, %s, %s, %s)',
                             (challenge_id, key, country, explanation))
        except Exception:
            container.delete_blob(key)
            raise
    return {'id': str(challenge_id)}


@app.post('/api/rounds', status_code=201)
def new_round():
    with db() as conn:
        challenge = conn.execute('SELECT id, country FROM challenges ORDER BY random() LIMIT 1').fetchone()
        if not challenge:
            raise HTTPException(404, 'No challenges yet. Upload the first photo!')
        options = random.sample([code for code in COUNTRIES if code != challenge['country']], 3)
        options.append(challenge['country'])
        random.shuffle(options)
        round_id = uuid4()
        conn.execute('INSERT INTO rounds (id, challenge_id, options) VALUES (%s, %s, %s)',
                     (round_id, challenge['id'], Jsonb(options)))
    return {'id': str(round_id), 'image_url': f'/api/rounds/{round_id}/image',
            'options': [{'code': code, 'name': COUNTRIES[code]} for code in options]}


@app.get('/api/rounds/{round_id}/image')
def image(round_id: UUID):
    with db() as conn:
        row = conn.execute('SELECT c.object_key FROM rounds r JOIN challenges c ON c.id = r.challenge_id WHERE r.id = %s',
                           (round_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Round not found.')
    with storage() as container:
        data = container.download_blob(row['object_key']).readall()
    return Response(data, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})


class Guess(BaseModel):
    country: str


@app.post('/api/rounds/{round_id}/guess')
def guess(round_id: UUID, body: Guess):
    with db() as conn:
        row = conn.execute('''SELECT r.*, c.country, c.explanation FROM rounds r
            JOIN challenges c ON c.id = r.challenge_id WHERE r.id = %s FOR UPDATE OF r''', (round_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Round not found.')
        if body.country not in row['options']:
            raise HTTPException(422, 'Choose one of the four options.')
        if row['guess'] is not None and body.country != row['guess']:
            raise HTTPException(409, 'This round has already been answered.')
        correct = body.country == row['country']
        if row['guess'] is None:
            conn.execute('UPDATE rounds SET guess = %s, correct = %s, answered_at = now() WHERE id = %s',
                         (body.country, correct, round_id))
    return {'correct': correct, 'country': COUNTRIES[row['country']],
            'country_code': row['country'], 'explanation': row['explanation']}
