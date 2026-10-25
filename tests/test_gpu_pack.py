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
    # Cards CTranslate2 only runs in float32: GTX 900 series (5.2) and Tesla P100 (6.0).
    assert "too old" in gpu_pack.why_not({**RTX, "compute": (5, 2)})
    assert "too old" in gpu_pack.why_not({**RTX, "compute": (6, 0)})
    assert gpu_pack.why_not({**RTX, "compute": (7, 5)}) == "" and gpu_pack.why_not({**RTX, "compute": (12, 0)}) == ""
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
    (tmp_path / "nvidia" / "nvidia_cudnn_cu12-9.1-win_amd64.whl.part").write_bytes(b"x" * 100)  # and an old download
    seen = []
    gpu_pack._install(m, lambda done, total, what: seen.append((done, total, what)))
    target = tmp_path / "nvidia" / "cuda12"
    assert sorted(p.name for p in target.iterdir()) == ["cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "pack.json"]
    assert not (tmp_path / "nvidia" / "cuda11").exists()
    assert not list((tmp_path / "nvidia").glob("*.part"))
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


def test_a_download_cut_off_is_kept_and_resumed_at_the_next_start(tmp_path, monkeypatch):
    """urllib just stops reading when the connection drops; the part must not be thrown away."""
    path = tmp_path / "x.whl.part"
    data = b"0123456789" * 1000

    class Cut:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self, n):
            chunk, self.body = self.body[:n], self.body[n:]
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(gpu_pack.urllib.request, "urlopen", lambda req, timeout: Cut(data[:4000]))
    sha = hashlib.sha256(data).hexdigest()
    with pytest.raises(OSError, match="next start"):
        gpu_pack._download("https://files.pythonhosted.org/x.whl", path, sha, len(data), lambda done: None)
    assert path.stat().st_size == 4000
    asked = []

    def resume(req, timeout):
        asked.append(req.get_header("Range"))
        r = Cut(data[4000:])
        r.status = 206
        return r

    monkeypatch.setattr(gpu_pack.urllib.request, "urlopen", resume)
    gpu_pack._download("https://files.pythonhosted.org/x.whl", path, sha, len(data), lambda done: None)
    assert asked == ["bytes=4000-"] and path.read_bytes() == data
