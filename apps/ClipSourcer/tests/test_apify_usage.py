from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

import requests

from clipsourcer.apify_client import fetch_tiktok_items
from clipsourcer.service import ClipSourcerService
from common.reporting import apify_usage, read_run_reporting, sourcing_run
from test_scoring import build_config


class ApifyUsageTests(unittest.TestCase):
    def test_actor_usage_counts_all_returned_clips_and_download_fallbacks(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp))
            response = mock.Mock()
            response.json.side_effect = [[{"id": str(i)} for i in range(25)], [{"id": "download"}]]
            with sourcing_run() as run_id, mock.patch("clipsourcer.apify_client.requests.post", return_value=response):
                fetch_tiktok_items(config)
                fetch_tiktok_items(config, include_video_downloads=True, post_urls=["https://example.com/video"])
            usage = apify_usage(config.output_dir, run_id=run_id)
            self.assertEqual(26, usage.requests)
            self.assertEqual(0, usage.unknown_calls)

    def test_failed_or_invalid_actor_responses_have_unknown_cost(self):
        for payload in [None, {"error": "bad shape"}]:
            with self.subTest(payload=payload), TemporaryDirectory() as tmp:
                config = build_config(Path(tmp))
                with sourcing_run() as run_id:
                    if payload is None:
                        post = mock.Mock(side_effect=requests.Timeout("timeout"))
                    else:
                        response = mock.Mock()
                        response.json.return_value = payload
                        post = mock.Mock(return_value=response)
                    with mock.patch("clipsourcer.apify_client.requests.post", post):
                        with self.assertRaises((requests.Timeout, ValueError)):
                            fetch_tiktok_items(config)
                self.assertEqual(1, apify_usage(config.output_dir, run_id=run_id).unknown_calls)

    def test_sourcing_metadata_survives_restart_and_cache_hit_has_zero_requests(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp))
            items = [{
                "id": str(i),
                "webVideoUrl": f"https://www.tiktok.com/@example/video/{i}",
                "text": "sample clip",
                "createTimeISO": datetime.now(timezone.utc).isoformat(),
                "authorMeta": {"name": "example"},
                "playCount": 50000,
            } for i in range(2)]
            response = mock.Mock()
            response.json.return_value = items
            service = ClipSourcerService(config)
            with mock.patch("clipsourcer.apify_client.requests.post", return_value=response) as post:
                first = service.run_once(bypass_schedule=True)
                second = ClipSourcerService(config).run_once(bypass_schedule=True)
            self.assertEqual(1, post.call_count)
            self.assertEqual("sourced", first.status)
            self.assertEqual("sourced", second.status)
            first_metadata = read_run_reporting(first.result.manifest_path.parent.parent)
            second_metadata = read_run_reporting(second.result.manifest_path.parent.parent)
            self.assertNotEqual(first_metadata["run_id"], second_metadata["run_id"])
            self.assertEqual(2, apify_usage(config.output_dir, run_id=first_metadata["run_id"]).requests)
            self.assertEqual(0, apify_usage(config.output_dir, run_id=second_metadata["run_id"]).requests)
            self.assertEqual([], json.loads(config.candidate_cache_file.read_text())["items"])
