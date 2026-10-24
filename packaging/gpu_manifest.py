"""Writes hyper_wispr/gpu_pack.json: which NVIDIA packages the installed app downloads on PCs
whose graphics card can use them (see hyper_wispr/gpu_pack.py). Run by release.yml before
PyInstaller, so each release pins exact versions and checksums.

    python packaging/gpu_manifest.py

The packages are the [gpu] extra in pyproject.toml, resolved to the newest matching Windows
wheel on PyPI.
"""

import json
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "hyper_wispr" / "gpu_pack.json"


def newest_wheel(req: Requirement) -> dict:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{req.name}/json", timeout=60) as resp:
        releases = json.loads(resp.read())["releases"]
    for version in sorted(releases, key=Version, reverse=True):
        v = Version(version)
        if v.is_prerelease or v not in req.specifier:
            continue
        for f in releases[version]:
            if f["filename"].endswith("-win_amd64.whl") and not f.get("yanked"):
                return {"name": req.name, "version": version, "url": f["url"], "sha256": f["digests"]["sha256"], "size": f["size"]}
    sys.exit(f"No Windows wheel on PyPI for {req}")


def main() -> None:
    extra = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["gpu"]
    reqs = [Requirement(r) for r in extra]
    wheels = [newest_wheel(r) for r in reqs]
    # e.g. nvidia-cublas-cu12 -> "cuda12": a new CUDA major version gets its own folder on the PC.
    cuda = sorted({m.group(1) for r in reqs if (m := re.search(r"-cu(\d+)$", r.name))})
    manifest = {"id": "cuda" + "-".join(cuda), "wheels": wheels}
    OUT.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    for w in wheels:
        print(f"{w['name']} {w['version']}: {w['size'] / 1e6:.0f} MB")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
