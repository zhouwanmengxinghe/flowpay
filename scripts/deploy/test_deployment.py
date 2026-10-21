"""Deployment regressions: credential persistence and invalid dataset rejection."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.deploy.config import create_config
from scripts.deploy.initialize import initialize_embedding, validate_dataset


class DeploymentTest(unittest.TestCase):
    def test_rerun_does_not_rotate_existing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(create_config(root))
            before = (root / ".env.deploy").read_bytes()
            self.assertFalse(create_config(root))
            self.assertEqual(before, (root / ".env.deploy").read_bytes())
            self.assertIn(b"FLOWPAY_MODEL_API_KEY=\n", before)

    def test_incomplete_dataset_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Incomplete unpacked dataset"):
                validate_dataset(Path(directory))

    def test_dataset_without_snapshot_requires_simulator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for part in ("data/knowledge", "benchmark/design", "postgres"):
                (root / part).mkdir(parents=True)
            (root / "postgres/flowpay_knowledge.dump").touch()
            with self.assertRaisesRegex(ValueError, "no business snapshot"):
                validate_dataset(root)
            (root / "src/flowpay_simulator").mkdir(parents=True)
            validate_dataset(root)

    def test_wrong_embedding_revision_stops_startup(self) -> None:
        config = b'[embedding]\nname="test-model"\nrevision="expected"\n'
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(json.dumps({
            "models": [{"name": "test-model", "digest": "wrong"}]
        }).encode())
        with patch("scripts.deploy.initialize.Path.open", return_value=io.BytesIO(config)), \
             patch("scripts.deploy.initialize.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "digest differs"):
                initialize_embedding()

    def test_model_download_error_stops_startup(self) -> None:
        config = b'[embedding]\nname="test-model"\nrevision="expected"\n'
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(b'{"models":[]}'),
                                  io.BytesIO(b'{"error":"download failed"}\n')]
        with patch("scripts.deploy.initialize.Path.open", return_value=io.BytesIO(config)), \
             patch("scripts.deploy.initialize.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                initialize_embedding()


if __name__ == "__main__":
    unittest.main()
