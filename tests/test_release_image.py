"""Multi-arch runtime image identity in the release metadata and notes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from release import main  # noqa: E402

INDEX = "sha256:" + "1" * 64
AMD64 = "sha256:" + "a" * 64
ARM64 = "sha256:" + "b" * 64
REFERENCE = f"ghcr.io/stevie1704/sil@{INDEX}"
REVISION = "0" * 40


def image_args(*platforms: str) -> list[str]:
    args = ["--image-reference", REFERENCE, "--image-digest", INDEX]
    for platform in platforms:
        args += ["--platform-digest", platform]
    return args


def bundle_args(tmp_path: Path) -> list[str]:
    """A release bundle with the native archive of each architecture."""
    bundle = tmp_path / "dist"
    bundle.mkdir(exist_ok=True)
    for architecture in ("x86_64", "aarch64"):
        (bundle / f"sil-native-dev-1.2.3-linux-{architecture}.tar.gz").write_bytes(b"")
    return ["--bundle", str(bundle)]


def test_release_metadata_gives_the_index_and_each_platform_digest(tmp_path: Path):
    output = tmp_path / "sil-release-1.2.3.json"

    status = main([
        "write-release-metadata", "--output", str(output),
        "--version", "1.2.3", "--source-revision", REVISION,
        *image_args(f"linux/amd64={AMD64}", f"linux/arm64={ARM64}"),
        *bundle_args(tmp_path),
    ])

    assert status == 0
    metadata = json.loads(output.read_text())
    assert metadata["image_reference"] == REFERENCE
    assert metadata["image_digest"] == INDEX
    assert metadata["image_platforms"] == {"linux/amd64": AMD64, "linux/arm64": ARM64}


def write_metadata(tmp_path: Path, *platforms: str) -> int:
    return main([
        "write-release-metadata", "--output", str(tmp_path / "release.json"),
        "--version", "1.2.3", "--source-revision", REVISION, *image_args(*platforms),
        *bundle_args(tmp_path),
    ])


def test_release_metadata_refuses_a_missing_platform(tmp_path: Path, capsys):
    assert write_metadata(tmp_path, f"linux/amd64={AMD64}") == 1
    assert "linux/arm64" in capsys.readouterr().err
    assert not (tmp_path / "release.json").exists()


def test_release_metadata_refuses_an_unknown_or_repeated_platform(tmp_path: Path):
    both = (f"linux/amd64={AMD64}", f"linux/arm64={ARM64}")
    assert write_metadata(tmp_path, *both, f"linux/riscv64={AMD64}") == 1
    assert write_metadata(tmp_path, *both, f"linux/arm64={AMD64}") == 1


def test_release_metadata_refuses_a_digest_that_is_not_sha256(tmp_path: Path, capsys):
    assert write_metadata(tmp_path, f"linux/amd64={AMD64}", "linux/arm64=pending") == 1
    assert "pending" in capsys.readouterr().err


def test_release_note_gives_each_platform_digest_and_says_to_pin_one(tmp_path: Path):
    output = tmp_path / "release-notes.md"

    status = main([
        "render-notes", "--template", str(ROOT / "docs" / "releases" / "template.md"),
        "--output", str(output), "--version", "1.2.3", "--source-revision", REVISION,
        *image_args(f"linux/amd64={AMD64}", f"linux/arm64={ARM64}"),
    ])

    assert status == 0
    lines = output.read_text().splitlines()
    assert any(INDEX in line for line in lines)
    for platform, digest in (("linux/amd64", AMD64), ("linux/arm64", ARM64)):
        assert any(platform in line and digest in line for line in lines), platform
    assert "pins one platform digest" in output.read_text()


def oci_index(*entries: tuple[str, str]) -> dict:
    manifests = []
    for platform, digest in entries:
        os_name, architecture = platform.split("/")
        manifests.append({
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": digest,
            "size": 1234,
            "platform": {"architecture": architecture, "os": os_name},
        })
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": manifests,
    }


def validate_index(tmp_path: Path, raw: str) -> int:
    path = tmp_path / "index.json"
    path.write_text(raw)
    return main([
        "validate-image-index", "--index", str(path),
        "--platform-digest", f"linux/amd64={AMD64}",
        "--platform-digest", f"linux/arm64={ARM64}",
    ])


def test_image_index_with_each_platform_image_is_accepted(tmp_path: Path):
    index = oci_index(("linux/arm64", ARM64), ("linux/amd64", AMD64))
    assert validate_index(tmp_path, json.dumps(index)) == 0


def test_image_index_that_selects_another_arm64_image_is_refused(tmp_path: Path, capsys):
    index = oci_index(("linux/amd64", AMD64), ("linux/arm64", AMD64))
    assert validate_index(tmp_path, json.dumps(index)) == 1
    assert "linux/arm64" in capsys.readouterr().err


def test_image_index_without_an_arm64_image_or_with_an_extra_image_is_refused(
    tmp_path: Path,
):
    only_amd64 = oci_index(("linux/amd64", AMD64))
    assert validate_index(tmp_path, json.dumps(only_amd64)) == 1
    extra = oci_index(("linux/amd64", AMD64), ("linux/arm64", ARM64),
                      ("linux/riscv64", "sha256:" + "c" * 64))
    assert validate_index(tmp_path, json.dumps(extra)) == 1


def test_unreadable_image_index_is_refused(tmp_path: Path):
    assert validate_index(tmp_path, "{not json") == 1


def test_release_workflow_builds_each_platform_image_on_its_own_runner():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    # A native build per architecture: no emulated build of the other one.
    assert "setup-qemu-action" not in workflow
    assert "--platform linux/amd64,linux/arm64" not in workflow
    for platform, runner in (("linux/amd64", "ubuntu-24.04"),
                             ("linux/arm64", "ubuntu-24.04-arm")):
        assert f"platform: {platform}\n            runner: {runner}\n" in workflow
    assert "push-by-digest=true" in workflow
    assert "docker buildx imagetools create" in workflow
    assert "tools/release.py validate-image-index" in workflow
    # The dry run before publication names its placeholder digests as such.
    assert "--image-digest pending --digests-pending" in workflow


def test_release_note_refuses_a_digest_that_is_not_sha256(tmp_path: Path, capsys):
    status = main([
        "render-notes", "--template", str(ROOT / "docs" / "releases" / "template.md"),
        "--output", str(tmp_path / "notes.md"), "--version", "1.2.3",
        "--source-revision", REVISION,
        *image_args(f"linux/amd64={AMD64}", "linux/arm64=sha256:short"),
    ])

    assert status == 1
    assert "sha256:short" in capsys.readouterr().err
    assert not (tmp_path / "notes.md").exists()


def test_release_note_accepts_pending_digests_only_when_asked(tmp_path: Path):
    def render(*extra: str) -> int:
        return main([
            "render-notes", "--template", str(ROOT / "docs" / "releases" / "template.md"),
            "--output", str(tmp_path / "notes.md"), "--version", "1.2.3",
            "--source-revision", REVISION, "--image-reference", REFERENCE,
            "--image-digest", "pending",
            "--platform-digest", "linux/amd64=pending",
            "--platform-digest", "linux/arm64=pending", *extra,
        ])

    assert render() == 1
    assert render("--digests-pending") == 0


def test_image_index_check_refuses_a_digest_that_is_not_sha256(tmp_path: Path, capsys):
    path = tmp_path / "index.json"
    # The index and the argument agree, so only the digest check can refuse it.
    path.write_text(json.dumps(oci_index(("linux/amd64", AMD64), ("linux/arm64", "short"))))

    status = main([
        "validate-image-index", "--index", str(path),
        "--platform-digest", f"linux/amd64={AMD64}",
        "--platform-digest", "linux/arm64=short",
    ])

    assert status == 1
    assert "'short'" in capsys.readouterr().err
