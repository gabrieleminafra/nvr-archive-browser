import json
import os
import sys
import tempfile
import unittest


APP = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.path.insert(0, APP)

import nvr_lib


class DiscoveryTests(unittest.TestCase):
    def make_camera(self, root, camera, session, ip, start):
        path = os.path.join(root, camera, session)
        os.makedirs(path)
        with open(os.path.join(path, 'session.json'), 'w') as f:
            json.dump({'mediaStreamOptions': {'url': 'rtsp://%s/live' % ip}}, f)
        segment = os.path.join(path, '%d.rtsp' % start)
        with open(segment, 'wb') as f:
            f.write(b'footage')

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

    def test_multiple_archives_group_same_physical_camera_by_ip(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_camera(os.path.join(root, 'old'), 'scrypted-26',
                             'session-a', '10.0.0.2', 2000)
            self.make_camera(os.path.join(root, 'recovered'), 'scrypted-42',
                             'session-b', '10.0.0.2', 1000)

            index = nvr_lib.build_index(
                root, ['old', 'recovered'], group_by_ip=True)

            self.assertEqual(list(index['cameras']), ['scrypted-26'])
            camera = index['cameras']['scrypted-26']
            self.assertEqual([s[0] for s in camera['segments']], [1000, 2000])
            self.assertEqual(camera['sources'], [
                {'archive': 'old', 'camera': 'scrypted-26'},
                {'archive': 'recovered', 'camera': 'scrypted-42'},
            ])
            self.assertTrue(camera['segments'][0][1].startswith('recovered/'))

    def test_archive_directory_cannot_escape_mount(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                nvr_lib.build_index(root, ['../elsewhere'])


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
