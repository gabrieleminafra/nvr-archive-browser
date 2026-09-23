import json
import os
import sys
import tempfile
import unittest


APP = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.path.insert(0, APP)

import nvr_lib


class DiscoveryTests(unittest.TestCase):
    def test_discovers_camera_and_ignores_companion_directories(self):
        with tempfile.TemporaryDirectory() as root:
            session = os.path.join(root, 'camera-a', 'session-1')
            os.makedirs(session)
            with open(os.path.join(session, 'session.json'), 'w') as f:
                json.dump({'mediaStreamOptions': {'url': 'rtsp://10.0.0.2/live'}}, f)
            for suffix in nvr_lib.SUFFIXES:
                os.makedirs(os.path.join(root, 'camera-a' + suffix))

            self.assertEqual(nvr_lib.discover_cameras(root), ['camera-a'])

    def test_camera_names_are_only_configured_by_environment(self):
        old = os.environ.get('NVR_CAMERA_NAMES')
        os.environ['NVR_CAMERA_NAMES'] = 'camera-a=Front Door,camera-b=Garage'
        try:
            self.assertEqual(nvr_lib.camera_names(), {
                'camera-a': 'Front Door',
                'camera-b': 'Garage',
            })
        finally:
            if old is None:
                os.environ.pop('NVR_CAMERA_NAMES', None)
            else:
                os.environ['NVR_CAMERA_NAMES'] = old


class TimelineTests(unittest.TestCase):
    def test_runs_collapse_nearby_segments_and_keep_gaps(self):
        segments = [
            (1_000, 'a', 1),
            (61_000, 'b', 1),
            (400_000, 'c', 1),
        ]
        self.assertEqual(nvr_lib.runs_of(segments), [
            [1_000, 121_000],
            [400_000, 460_000],
        ])


if __name__ == '__main__':
    unittest.main()
