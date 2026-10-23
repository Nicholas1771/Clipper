import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from PIL import Image

from clippublisher import instagram_client, media_storage
from clippublisher.config import AppConfig
from clippublisher.models import QueueItem


class InstagramStyleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.run = self.root / "clip"
        self.run.mkdir()
        self.item = QueueItem(
            run_dir=self.run, manifest_path=self.run / "manifest.json",
            video_path=self.run / "video.mp4", clip_id="1",
            caption="Source caption", author_handle="creator",
            description="Generated caption\n\n#clip",
        )
        self.config = AppConfig(
            queue_dir=self.root, poll_interval_seconds=1, ready_stability_seconds=0,
            instagram_user_id="ig", instagram_access_token="token",
            facebook_app_id="", facebook_app_secret="", token_state_file=self.root / "token.json",
            auto_refresh_token=False, refresh_buffer_days=10,
            media_base_url="https://media.example/", r2_account_id="",
            r2_access_key_id="", r2_secret_access_key="", r2_bucket_name="",
            r2_public_base_url="", r2_key_prefix="processing", caption_prefix="Prefix",
            streamer_hashtag="creator", timezone_name="UTC",
        )
        self.caption = self.root / "caption.txt"
        self.cover = self.root / "cover.jpg"
        Image.new("RGB", (90, 160), "red").save(self.cover)

    def test_fixed_caption_is_verbatim_without_prefix_or_hashtags(self):
        text = "\u65e5\u672c\u8a9e\n\nOnly this text.\n"
        self.caption.write_text(text, encoding="utf-8")
        config = replace(self.config, instagram_caption_file=self.caption)
        self.assertEqual(text, instagram_client.build_caption(config, self.item))

    def test_unconfigured_caption_preserves_existing_behavior(self):
        self.assertEqual(
            "Prefix Generated caption\n\n#creator #clip",
            instagram_client.build_caption(self.config, self.item),
        )

    def test_invalid_fixed_caption_does_not_fall_back(self):
        config = replace(self.config, instagram_caption_file=self.caption)
        for text in (" \n", "a" * 2201):
            self.caption.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                instagram_client.build_caption(config, self.item)
        self.caption.unlink()
        with self.assertRaises(FileNotFoundError):
            instagram_client.build_caption(config, self.item)

    def test_caption_at_limit_is_not_truncated(self):
        self.caption.write_text("a" * 2200, encoding="utf-8")
        config = replace(self.config, instagram_caption_file=self.caption)
        self.assertEqual("a" * 2200, instagram_client.build_caption(config, self.item))

    def test_cover_is_jpeg_and_served_with_local_media(self):
        config = replace(self.config, instagram_cover_image=self.cover)
        with mock.patch.object(instagram_client, "ensure_media_base_url", return_value=config.media_base_url):
            url = instagram_client.build_public_cover_url(config, self.item)
        self.assertEqual("https://media.example/clip/publisher/instagram_cover.jpg", url)
        with Image.open(self.run / "publisher" / "instagram_cover.jpg") as image:
            self.assertEqual("JPEG", image.format)
            self.assertEqual((90, 160), image.size)
        self.assertTrue(self.cover.is_file())
        self.assertEqual(self.cover.read_bytes(), (self.run / "publisher" / "instagram_cover.jpg").read_bytes())

    def test_cover_uses_r2_with_jpeg_content_type(self):
        config = replace(
            self.config, instagram_cover_image=self.cover, r2_account_id="account",
            r2_access_key_id="key", r2_secret_access_key="secret",
            r2_bucket_name="bucket", r2_public_base_url="https://r2.example/",
        )
        with mock.patch.object(media_storage.boto3, "client") as client:
            url = instagram_client.build_public_cover_url(config, self.item)
        self.assertEqual("https://r2.example/processing/clip/publisher/instagram_cover.jpg", url)
        args = client.return_value.upload_fileobj.call_args
        self.assertEqual("image/jpeg", args.kwargs["ExtraArgs"]["ContentType"])
        self.assertEqual("processing/clip/publisher/instagram_cover.jpg", args.args[2])

    def test_video_r2_upload_is_unchanged(self):
        self.item.video_path.write_bytes(b"video")
        config = replace(
            self.config, r2_account_id="account", r2_access_key_id="key",
            r2_secret_access_key="secret", r2_bucket_name="bucket",
            r2_public_base_url="https://r2.example/",
        )
        with mock.patch.object(media_storage.boto3, "client") as client:
            url = media_storage.upload_video_and_get_url(config, self.item)
        self.assertEqual("https://r2.example/processing/clip/video.mp4", url)
        self.assertEqual(
            {"ContentType": "video/mp4"},
            client.return_value.upload_fileobj.call_args.kwargs["ExtraArgs"],
        )

    def test_invalid_cover_fails_before_meta_request(self):
        self.cover.write_bytes(b"not an image")
        config = replace(self.config, instagram_cover_image=self.cover)
        with mock.patch.object(instagram_client, "build_public_video_url", return_value="https://media.example/video.mp4"), \
             mock.patch.object(instagram_client.requests, "post") as post:
            with self.assertRaises(OSError):
                instagram_client.publish_to_instagram(config, self.item, access_token="token")
        post.assert_not_called()

    def test_cover_size_limit_is_enforced(self):
        config = replace(self.config, instagram_cover_image=self.cover)
        for size in (8 * 1024 * 1024, 8 * 1024 * 1024 + 1):
            self.cover.write_bytes(self.cover.read_bytes() + b"x" * (size - self.cover.stat().st_size))
            with mock.patch.object(instagram_client.shutil, "copyfile"), \
                 mock.patch.object(instagram_client, "ensure_media_base_url", return_value=config.media_base_url):
                if size > 8 * 1024 * 1024:
                    with self.assertRaisesRegex(ValueError, "8 MB"):
                        instagram_client.build_public_cover_url(config, self.item)
                else:
                    self.assertTrue(instagram_client.build_public_cover_url(config, self.item).endswith(".jpg"))

    def test_png_cover_is_rejected(self):
        Image.new("RGB", (90, 160), "red").save(self.cover, format="PNG")
        with self.assertRaisesRegex(ValueError, "must be a JPEG"):
            instagram_client.build_public_cover_url(
                replace(self.config, instagram_cover_image=self.cover), self.item,
            )

    def test_media_request_only_sets_cover_when_configured(self):
        for cover in (None, self.cover):
            config = replace(self.config, instagram_cover_image=cover)
            with mock.patch.object(instagram_client, "build_public_video_url", return_value="https://media.example/video.mp4"), \
                 mock.patch.object(instagram_client, "build_public_cover_url", return_value="https://media.example/cover.jpg") as build, \
                 mock.patch.object(instagram_client, "get_media_status", return_value="FINISHED"), \
                 mock.patch.object(instagram_client.requests, "post", side_effect=[
                     mock.Mock(status_code=200, json=lambda: {"id": "container"}),
                     mock.Mock(status_code=200, json=lambda: {"id": "published"}),
                 ]) as post:
                result = instagram_client.publish_to_instagram(config, self.item, access_token="token")
                self.assertEqual({"id": "published"}, result)
                payload = post.call_args_list[0].kwargs["data"]
                if cover:
                    self.assertEqual("https://media.example/cover.jpg", payload["cover_url"])
                    build.assert_called_once()
                else:
                    self.assertNotIn("cover_url", payload)
                    build.assert_not_called()

    def test_config_resolves_optional_files_and_rejects_missing_files(self):
        self.caption.write_text("Fixed", encoding="utf-8")
        settings = {
            "INSTAGRAM_CAPTION_FILE": "caption.txt",
            "INSTAGRAM_COVER_IMAGE": str(self.cover),
            "MEDIA_BASE_URL": "https://media.example",
        }
        with mock.patch.dict(os.environ, settings, clear=True), \
             mock.patch("clippublisher.config.load_app_env", return_value=self.root), \
             mock.patch("clippublisher.config.configured_creators", return_value=()):
            config = AppConfig.from_repo_root(self.root, resolve_media_url=False)
            self.assertEqual(self.caption, config.instagram_caption_file)
            self.assertEqual(self.cover, config.instagram_cover_image)
            self.cover.unlink()
            with self.assertRaisesRegex(ValueError, "INSTAGRAM_COVER_IMAGE"):
                AppConfig.from_repo_root(self.root, resolve_media_url=False)

if __name__ == "__main__":
    unittest.main()
