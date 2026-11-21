import json

import pytest

from backend.services import youtube_publisher as yt


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(yt, 'config_path', lambda: tmp_path / 'youtube.json')
    monkeypatch.delenv('YOUTUBE_CLIENT_ID', raising=False)
    monkeypatch.delenv('YOUTUBE_CLIENT_SECRET', raising=False)
    yt._STATES.clear()
    return tmp_path


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


class Http:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)

    def post(self, url, **kw):
        return self._next('post', url, **kw)

    def put(self, url, **kw):
        return self._next('put', url, **kw)

    def get(self, url, **kw):
        return self._next('get', url, **kw)


def test_metadata_uses_the_generated_copy_and_marks_short_vertical_clips_as_shorts():
    post = {'title': 'T' * 150, 'description': 'Why this matters', 'tags': ['AI', 'ai', 'podcast interview', '#clips', 'x' * 600]}
    meta = yt.build_metadata(post, 'fallback', 42.0, True, 'private')
    assert len(meta['snippet']['title']) == 100
    assert meta['snippet']['tags'] == ['AI', 'podcast interview', 'clips']
    assert meta['snippet']['description'].endswith('#Shorts')
    assert '#AI #podcastinterview #clips' in meta['snippet']['description']
    assert meta['status'] == {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False, 'embeddable': True}
    long_clip = yt.build_metadata({}, 'Fallback title', 300.0, False, 'unlisted')
    assert long_clip['snippet']['title'] == 'Fallback title' and '#Shorts' not in long_clip['snippet']['description']


def test_settings_default_to_private_and_a_new_client_drops_the_old_connection():
    assert yt.load_config().privacy == 'private' and not yt.load_config().connected
    yt.save_settings(client_id='a', client_secret='b')
    data = json.loads(yt.config_path().read_text())
    data['refresh_token'] = 'tok'
    yt.config_path().write_text(json.dumps(data))
    assert yt.load_config().connected
    yt.save_settings(auto_post=True, privacy='unlisted')
    assert yt.load_config().connected
    yt.save_settings(client_id='other')
    assert not yt.load_config().connected
    with pytest.raises(ValueError):
        yt.save_settings(privacy='everyone')


def test_oauth_state_is_single_use_and_the_refresh_token_is_stored():
    yt.save_settings(client_id='cid', client_secret='sec')
    url = yt.auth_url('http://localhost:3000/cb')
    state = url.split('state=')[1].split('&')[0]
    assert 'access_type=offline' in url and 'youtube.upload' in url
    http = Http(Resp(200, {'refresh_token': 'r1', 'access_token': 'a1'}),
                Resp(200, {'items': [{'id': 'UC1', 'snippet': {'title': 'My channel'}}]}))
    cfg = yt.complete_auth('code', state, 'http://localhost:3000/cb', http)
    assert cfg.connected and cfg.channel_title == 'My channel'
    with pytest.raises(yt.YouTubeError):
        yt.complete_auth('code', state, 'http://localhost:3000/cb', Http())
    with pytest.raises(yt.YouTubeError):
        yt.complete_auth('code', 'forged', 'http://localhost:3000/cb', Http())


def test_revoked_access_says_to_reconnect():
    yt.save_settings(client_id='cid', client_secret='sec')
    data = json.loads(yt.config_path().read_text())
    data['refresh_token'] = 'old'
    yt.config_path().write_text(json.dumps(data))
    with pytest.raises(yt.YouTubeError, match='Connect YouTube again'):
        yt.access_token(http=Http(Resp(400, {'error': 'invalid_grant'})))


def test_upload_sends_metadata_then_the_file_and_reports_quota_errors(tmp_path):
    video = tmp_path / 'v.mp4'
    video.write_bytes(b'x' * 10)
    meta = yt.build_metadata({'title': 'Hello'}, '', 30.0, False, 'private')
    http = Http(Resp(200, headers={'Location': 'https://up/session'}), Resp(200, {'id': 'abc123', 'status': {'privacyStatus': 'private'}}))
    result = yt.upload_video(video, meta, 'tok', None, http)
    assert result == {'video_id': 'abc123', 'url': 'https://youtu.be/abc123', 'privacy': 'private'}
    assert json.loads(http.calls[0][2]['data'])['snippet']['title'] == 'Hello'
    assert http.calls[1][1] == 'https://up/session'
    quota = Http(Resp(403, {'error': {'message': 'x', 'errors': [{'reason': 'quotaExceeded'}]}}))
    with pytest.raises(yt.YouTubeError, match='daily YouTube API quota'):
        yt.upload_video(video, meta, 'tok', None, quota)
