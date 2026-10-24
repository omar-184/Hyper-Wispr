import hashlib
import json
import zipfile

import pytest

from hyper_wispr import gpu_pack, speech
from hyper_wispr.app import _download_label

MX130 = {"name": "NVIDIA GeForce MX130", "compute": (5, 0), "memory": 2 * 2**30 - 60 * 2**20, "driver": 12040}
MX150 = {"name": "NVIDIA GeForce MX150", "compute": (6, 1), "memory": 2 * 2**30 - 60 * 2**20, "driver": 12040}
RTX = {"name": "NVIDIA GeForce RTX 3060", "compute": (8, 6), "memory": 12 * 2**30, "driver": 12040}


def test_only_cards_that_can_run_whisper_qualify():
    assert "too old" in gpu_pack.why_not(MX130)  # Omar's laptop: stays on the processor
    assert gpu_pack.why_not(MX150) == ""  # 2 GB cards report a little under 2 GB
    assert gpu_pack.why_not(RTX) == ""
    assert "memory" in gpu_pack.why_not({**RTX, "memory": 1 * 2**30})
    assert "driver" in gpu_pack.why_not({**RTX, "driver": 11080})
    assert gpu_pack.usable_card([MX130]) is None
    assert gpu_pack.usable_card([MX130, RTX]) is RTX
    assert gpu_pack.usable_card([]) is None


def test_nothing_is_downloaded_for_the_processor_or_a_development_copy(monkeypatch):
    monkeypatch.setattr(gpu_pack, "nvidia_cards", lambda: [RTX])
    monkeypatch.setattr(gpu_pack, "manifest", lambda: {"id": "cuda12", "wheels": []})
    monkeypatch.setattr(gpu_pack, "is_frozen_windows", lambda: True)
    assert gpu_pack.wanted({"device": "auto"}) is RTX
    assert gpu_pack.wanted({"device": "cpu"}) is None
    monkeypatch.setattr(gpu_pack, "nvidia_cards", lambda: [MX130])
    assert gpu_pack.wanted({"device": "auto"}) is None
    assert gpu_pack.wanted({"device": "cuda"}) is None
    monkeypatch.setattr(gpu_pack, "nvidia_cards", lambda: [RTX])
    monkeypatch.setattr(gpu_pack, "is_frozen_windows", lambda: False)
    assert gpu_pack.wanted({"device": "auto"}) is None


def test_auto_uses_the_processor_until_the_nvidia_files_are_here(monkeypatch):
    monkeypatch.setattr(speech, "cuda_available", lambda: True)
    monkeypatch.setattr(speech, "gpu_compute_types", lambda: ["float16", "float32"])
    monkeypatch.setattr(gpu_pack, "is_frozen_windows", lambda: True)
    monkeypatch.setattr(gpu_pack, "folder", lambda m=None: None)
    assert speech.resolve({"model": "auto", "device": "auto", "compute_type": "auto"})[:2] == ("small", "cpu")
    monkeypatch.setattr(gpu_pack, "folder", lambda m=None: "somewhere")
    assert speech.resolve({"model": "auto", "device": "auto", "compute_type": "auto"})[:2] == ("large-v3-turbo", "cuda")


def _fake_wheel(tmp_path, name, files):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for f in files:
            z.writestr(f, b"dll " + f.encode())
    data = path.read_bytes()
    return {"name": name, "version": "1", "url": path.as_uri(), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def test_download_unpacks_only_the_dlls_and_checks_the_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_pack, "DOWNLOAD_PREFIX", "file:")
    monkeypatch.setattr(gpu_pack, "root", lambda: tmp_path / "nvidia")
    src = tmp_path / "src"
    src.mkdir()
    m = {"id": "cuda12", "wheels": [
        _fake_wheel(src, "cublas.whl", ["nvidia/cublas/bin/cublas64_12.dll", "nvidia/cublas/bin/cublasLt64_12.dll", "nvidia/cublas/include/cublas.h"]),
        _fake_wheel(src, "cudnn.whl", ["nvidia/cudnn/bin/cudnn64_9.dll", "nvidia/cudnn/lib/x64/cudnn.lib"]),
    ]}
    (tmp_path / "nvidia" / "cuda11").mkdir(parents=True)  # files for an older CUDA: removed
    seen = []
    gpu_pack._install(m, lambda done, total, what: seen.append((done, total, what)))
    target = tmp_path / "nvidia" / "cuda12"
    assert sorted(p.name for p in target.iterdir()) == ["cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "pack.json"]
    assert not (tmp_path / "nvidia" / "cuda11").exists()
    assert gpu_pack.folder(m) == target
    total = sum(w["size"] for w in m["wheels"])
    assert seen[-1] == (total, total, "gpu") and all(w == "gpu" for *_, w in seen)
    # A missing DLL means the files are incomplete and get downloaded again.
    (target / "cudnn64_9.dll").unlink()
    assert gpu_pack.folder(m) is None

    bad = dict(m["wheels"][0], sha256="0" * 64)
    with pytest.raises(OSError, match="checksum"):
        gpu_pack._install({"id": "cuda12", "wheels": [bad]}, None)
    assert not list((tmp_path / "nvidia").glob("*.part"))


def test_refuses_downloads_from_anywhere_but_pypi(tmp_path, monkeypatch):
    monkeypatch.setattr(gpu_pack, "root", lambda: tmp_path)
    m = {"id": "cuda12", "wheels": [{"name": "x", "version": "1", "url": "https://example.com/x.whl", "sha256": "0", "size": 1}]}
    with pytest.raises(ValueError):
        gpu_pack._install(m, None)


def test_ensure_never_raises(monkeypatch):
    monkeypatch.setattr(gpu_pack, "manifest", lambda: {"id": "cuda12", "wheels": []})
    monkeypatch.setattr(gpu_pack, "folder", lambda m=None: None)
    monkeypatch.setattr(gpu_pack, "wanted", lambda cfg: RTX)

    def boom(m, progress):
        raise OSError("no internet")

    monkeypatch.setattr(gpu_pack, "_install", boom)
    assert gpu_pack.ensure({"device": "auto"}) is False


def test_manifest_matches_the_gpu_extra():
    m = gpu_pack.manifest()
    if m is None:
        pytest.skip("gpu_pack.json is written by the release build")
    assert m["id"] == "cuda12"
    assert all(w["url"].startswith(gpu_pack.DOWNLOAD_PREFIX) and len(w["sha256"]) == 64 for w in m["wheels"])


def test_download_label_names_what_is_downloading():
    assert "NVIDIA" in _download_label({"done": 5e8, "total": 1e9, "what": "gpu"})
    assert "speech model" in _download_label({"done": 5e8, "total": 1e9, "what": "model"})
    assert "speech model" in _download_label({"done": 5e8, "total": 1e9})
