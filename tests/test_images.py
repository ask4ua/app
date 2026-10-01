import io

import pytest
from fastapi import HTTPException
from PIL import Image

from api.main import MAX_UPLOAD, normalize_image


def test_uploaded_metadata_is_removed_and_large_photo_resized():
    source = Image.new('RGB', (3000, 1600), 'green')
    exif = Image.Exif()
    exif[270] = 'Secret location: Ukraine'
    raw = io.BytesIO()
    source.save(raw, format='JPEG', exif=exif)
    result = Image.open(io.BytesIO(normalize_image(raw.getvalue())))
    assert result.size == (2400, 1280)
    assert not result.getexif()
    assert result.format == 'JPEG'


@pytest.mark.parametrize('data,status', [(b'', 413), (b'x' * (MAX_UPLOAD + 1), 413), (b'<svg></svg>', 415)])
def test_invalid_uploads_are_rejected(data, status):
    with pytest.raises(HTTPException) as error:
        normalize_image(data)
    assert error.value.status_code == status
