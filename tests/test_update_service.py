import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import update_service


class UpdateServiceTests(unittest.TestCase):
    def test_version_comparison(self):
        self.assertTrue(update_service.atualizacao_disponivel("1.0.7", "1.0.10"))
        self.assertFalse(update_service.atualizacao_disponivel("1.0.7", "1.0.7"))
        self.assertFalse(update_service.atualizacao_disponivel("1.1.0", "1.0.9"))
        self.assertFalse(update_service.atualizacao_disponivel("1.0.0", ""))

    def test_request_rejects_invalid_tag(self):
        for tag in ("", "latest", "v1.0", "v1.0.0; rm -rf /", "../v1.0.0"):
            with self.assertRaises(ValueError):
                update_service.solicitar_atualizacao(tag)

    def test_request_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with mock.patch.object(update_service, "DATA_DIR", data), \
                 mock.patch.object(update_service, "REQUEST_PATH", data / "update.request"), \
                 mock.patch.object(update_service, "STATUS_PATH", data / "update-status.json"):
                self.assertFalse(update_service.atualizacao_em_andamento())
                update_service.solicitar_atualizacao("v1.2.3")
                self.assertEqual((data / "update.request").read_text(), "v1.2.3")
                self.assertTrue(update_service.atualizacao_em_andamento())
                (data / "update.request").unlink()
                (data / "update-status.json").write_text(json.dumps({"state": "success", "message": "ok", "version": "1.2.3", "updated_at": 1}))
                self.assertEqual(update_service.ler_status_atualizacao()["state"], "success")
                self.assertFalse(update_service.atualizacao_em_andamento())

    def test_latest_release_parsing(self):
        payload = {
            "tag_name": "v1.2.0",
            "body": "notas",
            "html_url": "https://github.com/x/y/releases/tag/v1.2.0",
            "assets": [{"name": "TimelapseManager-Setup-1.2.0.exe", "browser_download_url": "https://example/setup.exe"}],
        }
        response = mock.Mock(**{"json.return_value": payload})
        with mock.patch.object(update_service.requests, "get", return_value=response):
            release = update_service._buscar_ultima_release()
        self.assertEqual(release["version"], "1.2.0")
        self.assertEqual(release["windows_installer_url"], "https://example/setup.exe")

    def test_latest_release_rejects_bad_tag(self):
        response = mock.Mock(**{"json.return_value": {"tag_name": "nightly"}})
        with mock.patch.object(update_service.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                update_service._buscar_ultima_release()


if __name__ == "__main__":
    unittest.main()
