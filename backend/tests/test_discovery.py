from backend.services.studio import discovery


def _heatmap(values, width=100.0):
    return [{'start_time': i * width, 'end_time': (i + 1) * width, 'value': v} for i, v in enumerate(values)]


def test_moments_are_the_strongest_replay_peaks_in_time_order():
    heat = _heatmap([0.2, 1.0, 0.9, 0.3, 0.1, 0.8, 0.2])
    moments = discovery.moments_from_heatmap(heat, duration=700)
    assert [(m['start'], m['end'], m['score']) for m in moments] == [(105.0, 195.0, 1.0), (205.0, 295.0, 0.9), (505.0, 595.0, 0.8)]
    assert all(m['source'] == 'most_replayed' for m in moments)


def test_a_peak_that_would_overlap_a_stronger_one_is_dropped_and_weak_values_give_nothing():
    narrow = discovery.moments_from_heatmap(_heatmap([0.2, 1.0, 0.9, 0.2], width=10.0), duration=100)
    assert [(m['start'], m['end'], m['score']) for m in narrow] == [(0.0, 30.0, 1.0)]
    assert discovery.moments_from_heatmap(_heatmap([0.1, 0.4, 0.5]), duration=300) == []


def test_windows_stay_inside_the_video():
    near_end = discovery.moments_from_heatmap(_heatmap([0.0, 0.0, 1.0], width=100.0), duration=300)
    assert near_end[0]['end'] <= 300 and near_end[0]['end'] - near_end[0]['start'] == 90


def test_chapters_are_the_fallback_and_short_ones_are_skipped():
    chapters = [{'start_time': 0, 'end_time': 20, 'title': 'Intro'}, {'start_time': 20, 'end_time': 400, 'title': 'Main'}]
    moments = discovery.moments_from_chapters(chapters, duration=400)
    assert [(m['start'], m['end'], m['label']) for m in moments] == [(20.0, 110.0, 'Main')]


def test_search_ranks_by_views_drops_short_and_live_and_flags_the_top(monkeypatch):
    def entry(identifier, views, duration, **extra):
        return {'id': identifier, 'title': identifier, 'view_count': views, 'duration': duration, **extra}
    entries = [
        entry('aaaaaaaaaaa', 10, 3000), entry('bbbbbbbbbbb', 5000, 3000), entry('ccccccccccc', 999999, 60),
        entry('ddddddddddd', 700, 3000, live_status='is_live'), entry('eeeeeeeeeee', 90000, 9000),
        entry('bbbbbbbbbbb', 5000, 3000),
    ]
    import backend.utils.download_recovery as recovery
    monkeypatch.setattr(recovery, 'extract_info_with_recovery', lambda url, options: {'entries': entries})
    results = discovery.search_videos('anything', limit=10)
    assert [r['url'][-11:] for r in results] == ['eeeeeeeeeee', 'bbbbbbbbbbb', 'aaaaaaaaaaa']
    assert [r['long_video'] for r in results] == [True, False, False]
    assert all(r['suggested'] for r in results)
