import csv
import io
import json

import geopandas as gpd
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from rasterio.transform import from_origin
from shapely.geometry import LineString

import drishx as app_module
from detection_quality import build_trends, observation_quality, select_observations, valid_data_mask, road_buffer_m
from drishx import AnalyzeRequest, ObjectExtractor, analysis_grid, build_feature_stack, classify, rf_classify


def scene(h=12, w=12):
    data = np.full((h, w, 7), 0.12, dtype=np.float32)
    data[:, :, 4] = 0
    data[:, :, 5] = 5
    data[:, :, 6] = 1
    return data


def test_road_features_do_not_depend_on_surrounding_land():
    data = scene()
    mask = np.zeros(data.shape[:2], bool)
    mask[5:7, :] = True
    data[5, 5, :4] = [0.1, 0.2, 0.3, 0.4]
    first = build_feature_stack(data, mask)['feature_stack']
    changed = data.copy()
    changed[~mask, :4] = 0.9
    second = build_feature_stack(changed, mask)['feature_stack']
    np.testing.assert_allclose(first, second, equal_nan=True)
    # Three RGB values with mean .2: sum squared deviations .02 / 4.
    assert first[0, 5, 5] == pytest.approx(0.005)
    assert np.isnan(first[:, ~mask]).all()


@pytest.mark.parametrize('scl', [0, 1, 3, 8, 9, 10, 11])
def test_scl_and_adjacent_mixed_pixels_excluded(scl):
    data = scene()
    data[5, 5, 5] = scl
    valid = valid_data_mask(data)
    assert not valid[4:7, 4:7].any()
    assert valid[1, 1]


@pytest.mark.parametrize('channel,value', [(0, np.nan), (1, np.inf), (2, 0), (4, 255), (6, 0)])
def test_invalid_reflectance_and_no_data_are_excluded(channel, value):
    data = scene()
    data[5, 5, channel] = value
    assert not valid_data_mask(data)[5, 5]


class FakeRF:
    # A permuted class order must yield the same semantic predictions.
    classes_ = np.array([4, 2, 1, 3])

    def predict_proba(self, values):
        assert values.shape == (1, 7)  # Only finite road pixels reach the RF.
        return np.array([[0.03, 0.80, 0.10, 0.07]])


def test_model_class_order_and_blue_seed_threshold():
    fs = np.zeros((7, 2, 2), dtype=np.float32)
    mask = np.array([[1, 0], [0, 0]])
    probs, labels = rf_classify(fs, mask, FakeRF())
    assert labels[0, 0] == 2
    assert np.count_nonzero(labels) == 1
    np.testing.assert_allclose(probs[:, 0, 0], [0.10, 0.80, 0.07, 0.03])
    strict_probs, strict_labels = rf_classify(fs, mask, FakeRF(), 'precision')
    assert strict_labels[0, 0] == 1
    np.testing.assert_array_equal(strict_probs, probs)  # Scores remain unmodified.


def test_no_silent_heuristic_fallback():
    with pytest.raises(ValueError, match='Trained RF model unavailable'):
        classify(np.zeros((7, 2, 2)), np.ones((2, 2)), None)


def signature(h=12, w=12, row=5, col=4, strength=.95):
    labels = np.ones((h, w), dtype=np.int8)
    probs = np.zeros((4, h, w), dtype=np.float32)
    probs[0] = 1
    for offset, value in enumerate([2, 3, 4]):
        labels[row, col + offset] = value
        probs[:, row, col + offset] = 0
        probs[value - 1, row, col + offset] = strength
        probs[0, row, col + offset] = 1 - strength
    return probs, labels


@pytest.mark.parametrize('row,col', [(0, 0), (11, 9), (5, 0), (0, 9), (5, 4)])
def test_valid_signatures_at_all_image_edges(row, col):
    probs, labels = signature(row=row, col=col)
    extractor = ObjectExtractor(probs, np.arange(12)[::-1], np.arange(12))
    found = extractor.extract(labels)
    assert len(found) == 1
    assert found[0]['lon'] == col + 1  # Pixel centres, no half-pixel shift.
    assert found[0]['lat'] == 11 - row


def test_weak_cluster_rejected_by_strict_profile():
    probs, labels = signature(strength=.7)
    assert len(ObjectExtractor(probs, np.arange(12), np.arange(12)).extract(labels)) == 1
    assert ObjectExtractor(probs, np.arange(12), np.arange(12), profile='precision').extract(labels) == []


def test_unrelated_red_in_bounding_box_cannot_validate_cluster():
    labels = np.ones((7, 7), np.int8)
    labels[2, 2], labels[3, 3], labels[2, 4], labels[2, 3] = 2, 3, 2, 4
    probs = np.full((4, 7, 7), .95)
    cluster = labels.copy()
    cluster[2, 2] = cluster[3, 3] = cluster[2, 4] = 100
    extractor = ObjectExtractor(probs, np.arange(7), np.arange(7))
    assert extractor._postprocess_cluster(cluster, labels, probs, labels[1:4, 1:4], 2, 2, 2, 2, 100) is None


def test_compass_wraps_to_north():
    assert ObjectExtractor._direction_to_compass(359) == 'N'


def test_coverage_is_measured_on_roads_not_whole_scene():
    data = scene()
    mask = np.zeros((12, 12), bool)
    mask[5:7] = True
    assert observation_quality(data, mask, '2026-10-01')['status'] == 'usable'
    data[5:7, :, 5] = 9
    obs = observation_quality(data, mask, '2026-10-01')
    assert obs['status'] == 'insufficient_coverage'
    assert obs['clear_road_fraction'] == 0
    assert obs['detection_count'] is None


def test_sampling_covers_whole_period_and_deduplicates_tiles():
    results = [{'id': str(i), 'properties': {'datetime': f'2026-09-{i:02}T10:00:00Z'}} for i in range(1, 31)]
    sampled = select_observations(results + results, 3)
    assert [o['id'] for o in sampled] == ['1', '15', '30']
    assert select_observations(results, 1)[0]['id'] == '30'


def test_unobserved_cloudy_and_failed_dates_are_null_but_clear_zero_is_zero():
    history = [
        {'mission_id': 'a', 'label': 'A', 'observations': [
            {'timestamp': '2026-10-01', 'status': 'usable', 'detection_count': 0},
            {'timestamp': '2026-10-02', 'status': 'insufficient_coverage', 'detection_count': None},
            {'timestamp': '2026-10-03', 'status': 'failed', 'detection_count': None}]},
        {'mission_id': 'b', 'label': 'B', 'observations': [
            {'timestamp': '2026-10-04', 'status': 'usable', 'detection_count': 3}]}]
    result = build_trends(history)
    assert result['datasets'][0]['data'] == [0, None, None, None]
    assert result['datasets'][1]['data'] == [None, None, None, 3]
    assert result['summary']['total_detections'] == 3
    assert result['summary']['excluded_observations'] == 2
    assert build_trends(history, from_date='2026-10-02', site_ids='a')['labels'] == ['2026-10-02', '2026-10-03']


def test_grid_is_metric_and_roads_use_research_widths():
    _, (width, height), transform = analysis_grid([52.25, 10.45, 52.32, 10.55])
    assert 0 < width <= 2500 and 0 < height <= 2500
    assert transform.a == 10 and transform.e == -10
    assert [road_buffer_m(x) for x in ['motorway', 'trunk', 'primary', 'secondary']] == [20, 15, 10, 0]
    assert road_buffer_m(['primary', 'trunk']) == 15


@pytest.mark.parametrize('kwargs', [
    {'bbox': [0, 0, 0, 1]}, {'bbox': [0, 0, 1]}, {'bbox': [0, 0, float('nan'), 1]},
    {'bbox': [0, 0, 1, 1], 'max_frames': 0}, {'bbox': [0, 0, 1, 1], 'profile': 'unknown'},
])
def test_invalid_analysis_inputs(kwargs):
    with pytest.raises(ValidationError):
        AnalyzeRequest(**kwargs)


def test_analysis_masks_requests_reports_failures_and_exports(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'DATA_DIR', str(tmp_path))
    bbox = [52.25, 10.45, 52.251, 10.451]
    grid, _, _ = analysis_grid(bbox)
    monkeypatch.setattr(app_module, 'analysis_grid', lambda _: (grid, (12, 12), from_origin(grid.min_x, grid.max_y, 10, 10)))
    roads = gpd.GeoDataFrame({'highway': ['motorway'], 'geometry': [LineString([(10.45, 52.2505), (10.451, 52.2505)])]}, crs='EPSG:4326')
    monkeypatch.setattr(app_module.engine, 'fetch_roads', lambda _: roads)
    monkeypatch.setattr(app_module.engine, 'rf_model', object())
    monkeypatch.setattr(app_module.engine, 'history', [])
    monkeypatch.setattr(app_module.engine, 'detect_trucks', lambda *args, **kwargs: [])

    class Catalog:
        def __init__(self, **kwargs): pass
        def search(self, *args, **kwargs):
            return [{'id': f'scene-{i}', 'properties': {'datetime': f'2026-10-0{i}T10:00:00Z'}} for i in (1, 2, 3)]

    real_request = app_module.SentinelHubRequest
    class Request:
        input_data = staticmethod(real_request.input_data)
        output_response = staticmethod(real_request.output_response)
        def __init__(self, **kwargs):
            assert kwargs['size'] == (12, 12)
            assert 'dataMask' in kwargs['evalscript'] and 'SCL' in kwargs['evalscript']
            self.date = kwargs['input_data'][0]['dataFilter']['timeRange']['from']
        def get_data(self, **kwargs):
            assert kwargs['save_data'] is True
            if self.date.startswith('2026-10-03'):
                raise RuntimeError('Simulated acquisition failure')
            data = scene()
            if self.date.startswith('2026-10-02'):
                data[:, :, 5] = 9
            return [data]
    monkeypatch.setattr(app_module, 'SentinelHubCatalog', Catalog)
    monkeypatch.setattr(app_module, 'SentinelHubRequest', Request)
    client = TestClient(app_module.app)
    events = [json.loads(line) for line in client.post('/api/analyze', json={'bbox': bbox}).text.splitlines()]
    assert events[-1]['type'] == 'result', events
    assert events[-1]['usable_observations'] == 1
    assert events[-1]['excluded_observations'] == 2
    trends = client.get('/api/analytics/trends').json()
    assert trends['datasets'][0]['data'] == [0, None, None]
    mission_id = events[-1]['mission_id']
    response = client.get(f'/api/missions/{mission_id}/export')
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [r['detection_count'] for r in rows] == ['0', '', '']
    assert [r['status'] for r in rows] == ['usable', 'insufficient_coverage', 'failed']
    assert client.get('/api/missions/missing/export').status_code == 404


def test_real_model_smoke_on_uniform_road():
    if app_module.engine.rf_model is None:
        pytest.skip('Git LFS RF model unavailable')
    data = scene(20, 20)
    fs = build_feature_stack(data, np.ones((20, 20)))['feature_stack']
    probs, labels = rf_classify(fs, np.ones((20, 20)), app_module.engine.rf_model)
    assert np.isfinite(probs).all()
    assert ObjectExtractor(probs, np.arange(20), np.arange(20)).extract(labels) == []


def test_detection_uses_projected_pixel_centre_coordinates():
    from pyproj import Transformer
    probs, labels = signature()
    transform = from_origin(500000, 5800000, 10, 10)
    det = ObjectExtractor(probs, transform=transform, crs='EPSG:32632').extract(labels)[0]
    expected_lon, expected_lat = Transformer.from_crs('EPSG:32632', 'EPSG:4326', always_xy=True).transform(500055, 5799945)
    assert det['lat'] == pytest.approx(expected_lat)
    assert det['lon'] == pytest.approx(expected_lon)


def test_roads_use_bounded_requests_and_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'DATA_DIR', str(tmp_path))
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {'elements': [{'type': 'way', 'id': 42, 'tags': {'highway': 'motorway'},
                                      'geometry': [{'lon': 10.5, 'lat': 52.34}, {'lon': 10.6, 'lat': 52.35}]}]}
        return Response()
    monkeypatch.setattr(app_module.requests, 'post', post)
    bbox = [52.33, 10.5, 52.36, 10.6]
    roads = app_module.engine.fetch_roads(bbox)
    assert len(roads) == 1 and roads.iloc[0].osmid == 42
    assert calls[0][1]['timeout'] == (5, 20)
    assert len(app_module.engine.fetch_roads(bbox)) == 1
    assert len(calls) == 1


def test_all_road_mirrors_fail_with_visible_error(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'DATA_DIR', str(tmp_path))
    def post(*args, **kwargs):
        raise app_module.requests.Timeout()
    monkeypatch.setattr(app_module.requests, 'post', post)
    with pytest.raises(RuntimeError, match='temporarily unavailable'):
        app_module.engine.fetch_roads([52.33, 10.5, 52.36, 10.6])


def test_small_road_extract_is_cached_after_overpass_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, 'DATA_DIR', str(tmp_path))
    def unavailable(*args, **kwargs):
        raise app_module.requests.Timeout()
    monkeypatch.setattr(app_module.requests, 'post', unavailable)
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        class Response:
            content = b'<osm><node id="1" lat="52.34" lon="10.54"/><node id="2" lat="52.35" lon="10.55"/><way id="7"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/></way></osm>'
            def raise_for_status(self): pass
        return Response()
    monkeypatch.setattr(app_module.requests, 'get', get)
    bbox = [52.337, 10.53, 52.35, 10.56]
    assert len(app_module.engine.fetch_roads(bbox)) == 1
    assert len(app_module.engine.fetch_roads(bbox)) == 1
    assert len(calls) == 1
