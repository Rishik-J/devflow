"""Stamp static asset URLs so a new Pages HTML document loads its matching CSS and JS."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def main(site: Path) -> str:
    digest = hashlib.sha256()
    for name in ("styles.css", "app.js"):
        digest.update((site / name).read_bytes())
    version = digest.hexdigest()[:12]
    index = site / "index.html"
    html = index.read_text(encoding="utf-8")
    if html.count("__ASSET_VERSION__") != 2:
        raise ValueError("expected exactly two site asset placeholders")
    index.write_text(html.replace("__ASSET_VERSION__", version), encoding="utf-8")
    return version


if __name__ == "__main__":
    site = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "site"
    print(f"site assets: {main(site)}")
