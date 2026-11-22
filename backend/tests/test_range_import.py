import pytest
from pydantic import ValidationError

from backend.services.studio import range_import
from backend.services.studio.models import ImportOptions

SRT = """1
00:00:01,000 --> 00:00:03,000
before the range

2
01:00:00,000 --> 01:00:04,500
starts the range

3
01:00:09,000 --> 01:00:20,000
runs past the range end

4
02:00:00,000 --> 02:00:02,000
after the range
"""


def test_trim_srt_shifts_cues_to_the_range_and_clips_the_last_one():
    text, count = range_import.trim_srt(SRT, 3600, 3615)
    assert count == 2
    assert '00:00:00,000 --> 00:00:04,500\nstarts the range' in text
    assert '00:00:09,000 --> 00:00:15,000\nruns past the range end' in text
    assert 'before the range' not in text and 'after the range' not in text


def test_trim_srt_keeps_a_cue_that_straddles_the_range_start():
    text, count = range_import.trim_srt(SRT, 3602, 3615)
    assert count == 2
    assert text.startswith('1\n00:00:00,000 --> 00:00:02,500\nstarts the range')


@pytest.mark.parametrize('url,expected', [
    ('https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=30', {'site': 'youtube', 'id': 'aqz-KE-bpKQ'}),
    ('https://youtu.be/aqz-KE-bpKQ?si=x', {'site': 'youtube', 'id': 'aqz-KE-bpKQ'}),
    ('https://www.youtube.com/live/aqz-KE-bpKQ', {'site': 'youtube', 'id': 'aqz-KE-bpKQ'}),
    ('https://www.bilibili.com/video/BV1xx411c7mD?p=3', {'site': 'bilibili', 'id': 'BV1xx411c7mD', 'page': 3}),
    ('https://www.youtube.com/watch?v=short', None),
    ('https://example.com/watch?v=aqz-KE-bpKQ', None),
])
def test_embed_info(url, expected):
    assert range_import.embed_info(url) == expected


def test_only_videos_over_ninety_minutes_are_long():
    assert not range_import.is_long(90 * 60)
    assert range_import.is_long(90 * 60 + 1)
    assert not range_import.is_long(None)


def test_range_limits_and_overlap_are_refused():
    with pytest.raises(ValidationError):
        range_import.SourceRange(start=100, end=105)
    with pytest.raises(ValidationError):
        range_import.SourceRange(start=0, end=range_import.MAX_RANGE_SEC + 1)
    with pytest.raises(ValidationError):
        range_import.ImportRangesBody(url='https://youtu.be/aqz-KE-bpKQ', ranges=[
            {'start': 100, 'end': 200}, {'start': 150, 'end': 300}])
    body = range_import.ImportRangesBody(url='https://youtu.be/aqz-KE-bpKQ', ranges=[
        {'start': 100, 'end': 200}, {'start': 200, 'end': 300}])
    assert len(body.ranges) == 2


def test_import_options_carry_the_range_into_the_plan_overrides():
    options = ImportOptions(goal='content', source_range={'start': 3600, 'end': 3700, 'label': 'Q&A'})
    assert options.model_dump(exclude_none=True)['source_range'] == {'start': 3600.0, 'end': 3700.0, 'label': 'Q&A'}


def _range_drafts(monkeypatch, source_range):
    from backend.services.studio import jobs
    monkeypatch.setattr(jobs, 'run_content', lambda *_: [
        {'generated_title': 'low', 'start_time_seconds': 5, 'end_time_seconds': 20, 'final_score': 0.3},
        {'generated_title': 'best', 'start_time_seconds': 30, 'end_time_seconds': 50, 'final_score': 0.9},
    ])
    monkeypatch.setattr(jobs.intelligence, '_probe', lambda _: {'duration': 100})
    plan = {'preferences': {'goal': 'content'}, 'overrides': {'source_range': source_range} if source_range else {}}
    return jobs._content_drafts('p', plan, 'video.mp4')


def test_a_picked_range_becomes_exactly_one_clip_covering_the_whole_download(monkeypatch):
    result = _range_drafts(monkeypatch, {'start': 3600.0, 'end': 3700.0, 'label': ''})
    assert [(d['title'], d['scenes'][0]['start'], d['scenes'][0]['end']) for d in result] == [('best', 0.0, 100.0)]


def test_a_picked_range_uses_the_title_the_user_typed(monkeypatch):
    result = _range_drafts(monkeypatch, {'start': 3600.0, 'end': 3700.0, 'label': 'The announcement'})
    assert [d['title'] for d in result] == ['The announcement']


def test_a_picked_range_still_becomes_a_clip_when_content_analysis_fails(monkeypatch):
    from backend.services.studio import jobs, store
    def broken(*_):
        raise RuntimeError('API key invalid')
    monkeypatch.setattr(jobs, 'run_content', broken)
    monkeypatch.setattr(jobs.intelligence, '_probe', lambda _: {'duration': 40})
    monkeypatch.setattr(store, 'read', lambda _: {'source_meta': {'title': 'GTA 6 breakdown'}})
    plan = {'preferences': {'goal': 'content'}, 'overrides': {'source_range': {'start': 100.0, 'end': 140.0, 'label': ''}}}
    result = jobs._content_drafts('p', plan, 'video.mp4')
    assert [(d['title'], d['scenes'][0]['start'], d['scenes'][0]['end']) for d in result] == [('GTA 6 breakdown', 0.0, 40.0)]
    with pytest.raises(RuntimeError):
        jobs._content_drafts('p', {'preferences': {'goal': 'content'}, 'overrides': {}}, 'video.mp4')


def test_gameplay_stretch_keeps_reaction_facecam_around_real_gameplay_and_refuses_the_rest():
    from backend.services.studio import gameplay
    times = [1.25 + 2.5 * i for i in range(12)]
    labels = ['trailer', 'trailer', 'gameplay', 'gameplay', 'other', 'gameplay', 'gameplay', 'gameplay', 'facecam', 'facecam', 'gameplay', 'trailer']
    assert gameplay.longest_gameplay(times, labels, 30.0) == (5.0, 27.5)  # the face-cam cut-ins stay, the trailer ends are cut
    assert gameplay.longest_gameplay(times, ['trailer'] * 12, 30.0) is None
    assert gameplay.longest_gameplay(times, ['facecam'] * 12, 30.0) is None  # a pure reaction is not a gameplay clip
    assert gameplay.longest_gameplay(times, ['facecam'] * 9 + ['gameplay', 'facecam', 'facecam'], 30.0) is None  # one game frame in a talking head
    assert gameplay.longest_gameplay(times, ['gameplay', 'gameplay'] + ['other'] * 10, 30.0) is None  # 5 s is too short to be a clip


def test_classify_treats_missing_or_unknown_labels_as_other(monkeypatch):
    from backend.services.studio import gameplay
    monkeypatch.setattr(gameplay, '_endpoint', lambda: {'base_url': 'https://x/v1', 'api_key': 'k', 'model': 'm'})

    class Resp:
        status_code = 200
        def json(self):
            return {'choices': [{'message': {'content': '```json\n{"labels": ["gameplay", "Trailer", "weird"]}\n```'}}]}

    class Http:
        def post(self, *a, **k):
            return Resp()
    assert gameplay.classify([b'1', b'2', b'3', b'4'], Http()) == ['gameplay', 'trailer', 'other', 'other']


def test_queue_rows_show_stage_percent_and_tell_skipped_from_failed():
    from types import SimpleNamespace
    from backend.api.v1.studio import _queue_item
    project = SimpleNamespace(id='p1', name='Clip', created_at=None)
    def row(analysis, variants=()):
        return _queue_item(project, {'analysis': analysis, 'output_variants': list(variants)})
    assert row({})['kind'] == 'queued'
    downloading = row({'status': 'running', 'phase': 'screening', 'percent': 50})
    assert (downloading['stage'], downloading['percent']) == ('downloading', 20)
    assert row({'status': 'running', 'phase': 'production'})['stage'] == 'analyzing'
    rendering = row({'status': 'running', 'phase': 'rendering'}, [{'status': 'queued'}])
    assert (rendering['stage'], rendering['percent']) == ('rendering', 80)
    assert row({'status': 'completed'})['percent'] == 100
    skipped = row({'status': 'failed', 'error': 'No gameplay found in this part (it shows a trailer). Skipped.'})
    assert skipped['kind'] == 'skipped' and 'No gameplay' in skipped['error']
    assert row({'status': 'failed', 'error': 'Something broke'})['kind'] == 'failed'


def test_ledger_repeat_rule_and_discovery_takes_the_next_best_peaks():
    from backend.services.studio import discovery, ledger
    assert ledger.video_key('https://www.youtube.com/watch?v=E4RGVl86E2U&t=5') == ledger.video_key('https://youtu.be/E4RGVl86E2U')
    assert ledger.is_repeat(100, 130, [(105, 135)]) and not ledger.is_repeat(100, 130, [(200, 230)])
    heat = [{'start_time': i * 10.0, 'end_time': i * 10.0 + 10, 'value': 1.0 - i * 0.02} for i in range(20)]
    first = discovery.moments_from_heatmap(heat, 200.0)
    second = discovery.moments_from_heatmap(heat, 200.0, [(m['start'], m['end']) for m in first])
    assert first and second
    assert not any(ledger.is_repeat(m['start'], m['end'], [(f['start'], f['end']) for f in first]) for m in second)
