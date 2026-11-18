"""GPU-010: HF downloads must not die on huggingface_hub's symlink-probe race on Windows."""

import sys
import unittest
from unittest import mock

from backend.engines import hf_cache


class AvoidSymlinkRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        import huggingface_hub.file_download  # noqa: F401 - the package loads submodules lazily; mock.patch needs it
        from huggingface_hub import constants

        self.constants = constants
        original = constants.HF_HUB_DISABLE_SYMLINKS
        self.addCleanup(setattr, constants, "HF_HUB_DISABLE_SYMLINKS", original)
        constants.HF_HUB_DISABLE_SYMLINKS = False

    def test_without_symlink_support_the_hub_is_put_in_copy_mode_before_any_thread_probes(self) -> None:
        with mock.patch.object(hf_cache.sys, "platform", "win32"), \
                mock.patch("huggingface_hub.file_download.are_symlinks_supported", return_value=False) as probe:
            self.assertTrue(hf_cache.avoid_symlink_race())
        probe.assert_called_once_with(self.constants.HF_HUB_CACHE)
        self.assertTrue(self.constants.HF_HUB_DISABLE_SYMLINKS)
        # What the download threads see: copy mode, decided without a per-repo probe.
        from huggingface_hub.file_download import are_symlinks_supported
        self.assertFalse(are_symlinks_supported("C:/some/new/repo/folder"))

    def test_symlink_capable_machines_are_left_alone(self) -> None:
        with mock.patch.object(hf_cache.sys, "platform", "win32"), \
                mock.patch("huggingface_hub.file_download.are_symlinks_supported", return_value=True):
            self.assertFalse(hf_cache.avoid_symlink_race())
        self.assertFalse(self.constants.HF_HUB_DISABLE_SYMLINKS)

    def test_other_platforms_are_untouched(self) -> None:
        with mock.patch.object(hf_cache.sys, "platform", "linux"), \
                mock.patch("huggingface_hub.file_download.are_symlinks_supported") as probe:
            self.assertFalse(hf_cache.avoid_symlink_race())
        probe.assert_not_called()

    def test_imagegen_install_applies_it_before_downloading(self) -> None:
        from backend.imagegen import backend_diffusers as backend

        order: list[str] = []
        fake_diffusers = mock.MagicMock()
        fake_diffusers.DiffusionPipeline.download.side_effect = lambda *a, **k: order.append("download") or "snap"
        with mock.patch.object(backend, "require_available"), \
                mock.patch.dict(sys.modules, {"diffusers": fake_diffusers}), \
                mock.patch("backend.engines.hf_cache.avoid_symlink_race",
                           side_effect=lambda: order.append("guard") or True):
            self.assertEqual(backend.install("stabilityai/sdxl-turbo"), "snap")
        self.assertEqual(order, ["guard", "download"])


if __name__ == "__main__":
    unittest.main()
