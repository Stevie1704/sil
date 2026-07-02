"""External behavior at the run boundary: invoke the runner with a manifest
and artifacts, assert on exit code and MCAP content only."""


class TestManifestRejection:
    def test_invalid_json_is_config_error(self, run_sil, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "manifest" in proc.stderr.lower()

    def test_missing_file_is_config_error(self, run_sil, tmp_path):
        proc = run_sil(tmp_path / "does_not_exist.json")
        assert proc.returncode == 2

    def test_wrong_manifest_version_is_config_error(self, run_sil, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('{"sil_manifest": 99}')
        proc = run_sil(bad)
        assert proc.returncode == 2
        assert "sil_manifest" in proc.stderr
