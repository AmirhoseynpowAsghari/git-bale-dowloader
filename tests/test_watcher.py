import os
import unittest
from unittest.mock import Mock, patch

import watcher


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "GITHUB_TOKEN": "TEST_GH_TOKEN",
            "BALE_BOT_TOKEN": "TEST_BALE_TOKEN",
            "BALE_CHAT_ID": "-1001",
            "GITHUB_REPOSITORY": "owner/bot",
            "GITHUB_SHA": "c" * 40,
            "MONITOR_REPO": "owner/target",
            "MONITOR_BRANCH": "main",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.github = Mock()
        self.bale = Mock()
        self.github.get_head.return_value = "b" * 40
        self.gh_patch = patch.object(watcher, "GithubClient", return_value=self.github)
        self.bale_patch = patch.object(watcher, "BaleClient", return_value=self.bale)
        self.gh_patch.start()
        self.bale_patch.start()
        self.addCleanup(self.gh_patch.stop)
        self.addCleanup(self.bale_patch.stop)

    def test_first_run_only_baselines(self):
        self.github.get_state.return_value = (None, None)
        watcher.run()
        self.bale.send_message.assert_called_once()
        self.bale.send_document.assert_not_called()
        self.github.save_state.assert_called_once_with(
            "owner/bot", {"repo": "owner/target", "branch": "main", "last_sha": "b" * 40}, None
        )

    def test_no_change_no_messages(self):
        self.github.get_state.return_value = (
            {"repo": "owner/target", "branch": "main", "last_sha": "b" * 40}, "fileblobsha"
        )
        watcher.run()
        self.bale.send_message.assert_not_called()
        self.github.save_state.assert_not_called()

    def test_changed_file_sent_and_deleted_reported(self):
        self.github.get_state.return_value = (
            {"repo": "owner/target", "branch": "main", "last_sha": "a" * 40}, "fileblobsha"
        )
        self.github.compare.return_value = {"files": [
            {"filename": "src/example.txt", "status": "modified", "sha": "e" * 40, "size": 5},
            {"filename": "old.txt", "status": "removed"},
        ]}
        self.github.download_blob.return_value = b"hello"
        watcher.run()
        self.bale.send_document.assert_called_once()
        args, _ = self.bale.send_document.call_args
        self.assertEqual(args[0], "example.txt")
        self.assertEqual(args[1], b"hello")
        self.assertIn("src/example.txt", args[2])
        self.assertIn("old.txt", self.bale.send_message.call_args.args[0])
        self.github.save_state.assert_called_once()

    def test_upload_failure_does_not_advance_state(self):
        self.github.get_state.return_value = (
            {"repo": "owner/target", "branch": "main", "last_sha": "a" * 40}, "fileblobsha"
        )
        self.github.compare.return_value = {"files": [
            {"filename": "a.txt", "status": "added", "sha": "e" * 40, "size": 1}
        ]}
        self.github.download_blob.return_value = b"x"
        self.bale.send_document.side_effect = RuntimeError("Network failure")
        with self.assertRaisesRegex(RuntimeError, "Network failure"):
            watcher.run()
        self.github.save_state.assert_not_called()

    def test_large_file_skipped_and_reported(self):
        self.github.get_state.return_value = (
            {"repo": "owner/target", "branch": "main", "last_sha": "a" * 40}, "sha"
        )
        self.github.compare.return_value = {"files": [
            {"filename": "big.bin", "status": "added", "sha": "e" * 40,
             "size": watcher.MAX_UPLOAD_BYTES + 1}
        ]}
        watcher.run()
        self.github.download_blob.assert_not_called()
        self.bale.send_document.assert_not_called()
        self.assertIn("big.bin", self.bale.send_message.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
