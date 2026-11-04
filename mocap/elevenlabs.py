"""ElevenLabs speech-to-speech, matching the face-mocap audio sidebar."""
from pathlib import Path
import json
import uuid
import urllib.request
import urllib.error

VOICES_URL = 'https://api.elevenlabs.io/v1/voices'
MODELS_URL = 'https://api.elevenlabs.io/v1/models'


def _request(url, key, data=None, content_type=None):
    headers = {'xi-api-key': key.strip()}
    if content_type:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method='POST' if data is not None else 'GET')
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            body = response.read()
            return response.headers.get('Content-Type', ''), body
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:500]
        if exc.code == 401:
            raise ValueError('Invalid ElevenLabs API key') from None
        raise ValueError(detail or f'ElevenLabs HTTP {exc.code}') from None
    except urllib.error.URLError as exc:
        raise ValueError('Cannot reach ElevenLabs: ' + str(exc.reason)) from None


def fetch_voices(key):
    _ctype, body = _request(VOICES_URL, key)
    data = json.loads(body.decode('utf-8'))
    return [{'voice_id': v['voice_id'], 'name': v.get('name') or v['voice_id']} for v in data.get('voices') or []]


def fetch_models(key):
    _ctype, body = _request(MODELS_URL, key)
    data = json.loads(body.decode('utf-8'))
    rows = data if isinstance(data, list) else []
    convertible = [row for row in rows if row.get('can_do_voice_conversion')]
    source = convertible or rows
    models = [{'model_id': row['model_id'], 'name': (row.get('name') or row['model_id']).strip()}
              for row in source if row.get('model_id')]
    return sorted(models, key=lambda row: row['name'].lower())


def _multipart(fields, filename, payload, mime):
    boundary = 'RGBMoCap' + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode('utf-8'))
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="{filename}"\r\n'
        f'Content-Type: {mime}\r\n\r\n'.encode('utf-8'))
    chunks.append(payload)
    chunks.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(chunks), 'multipart/form-data; boundary=' + boundary


def convert(key, voice_id, audio_path, options=None):
    options = options or {}
    audio_path = Path(audio_path)
    fields = {}
    if options.get('model_id'):
        fields['model_id'] = str(options['model_id'])
    if options.get('remove_background_noise') is not None:
        fields['remove_background_noise'] = 'true' if options['remove_background_noise'] else 'false'
    settings = {}
    if options.get('stability') is not None:
        settings['stability'] = float(options['stability'])
    if options.get('similarity_boost') is not None:
        settings['similarity_boost'] = float(options['similarity_boost'])
    if settings:
        fields['voice_settings'] = json.dumps(settings)
    mime = 'audio/wav' if audio_path.suffix.lower() == '.wav' else 'application/octet-stream'
    body, content_type = _multipart(fields, audio_path.name, audio_path.read_bytes(), mime)
    query = ''
    if options.get('output_format'):
        query = '?output_format=' + urllib.request.quote(str(options['output_format']))
    url = f'https://api.elevenlabs.io/v1/speech-to-speech/{urllib.request.quote(voice_id)}{query}'
    ctype, payload = _request(url, key, body, content_type)
    suffix = '.wav' if 'wav' in ctype or str(options.get('output_format', '')).startswith('pcm') else '.mp3'
    return payload, suffix
