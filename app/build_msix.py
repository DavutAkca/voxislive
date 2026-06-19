#!/usr/bin/env python3
"""
Voxis MSIX packager (Microsoft Store)
-------------------------------------
Wraps the PyInstaller --onedir official bundle (dist/VoxisLive) into an
unsigned .msix for the Microsoft Store. The Store re-signs MSIX packages with
its own certificate, so no code-signing certificate is needed here.

Run AFTER app/build_official.py has produced dist/VoxisLive (so the package
includes the latest source, e.g. the VB-CABLE optional-framing changes).

Pipeline:
  1. Stage dist/VoxisLive into a clean layout dir (VoxisLive.exe at root).
  2. Generate the required tile/logo PNGs from app/assets/voxis.png.
  3. Write AppxManifest.xml (identity from Partner Center, runFullTrust, x64).
  4. makeappx pack -> Voxis_<version>.msix.

Identity values are the public Product Identity from Partner Center (visible in
the Store) — not secrets. The Publisher CN must match exactly or the upload is
rejected.
"""

import os
import re
import shutil
import subprocess
import pathlib

from PIL import Image

# --- Paths ---
APP_DIR = pathlib.Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
SRC_BUNDLE = ROOT_DIR / "dist" / "VoxisLive"          # produced by build_official.py
SRC_IMAGE = APP_DIR / "assets" / "voxis.png"
OUT_DIR = ROOT_DIR / "production_release"
LAYOUT_DIR = ROOT_DIR / "build" / "msix_layout"
ASSETS_DIR = LAYOUT_DIR / "Assets"

# --- Partner Center Product Identity (public; "Voxis" MSIX product) ---
IDENTITY_NAME = "Voxis.Voxis"
PUBLISHER = "CN=B793784D-B600-465E-9306-01ACA6831D2A"
PUBLISHER_DISPLAY = "Voxis"
APP_DISPLAY = "Voxis"
APP_DESCRIPTION = (
    "Real-time voice translation for Windows — translate any video, game, or "
    "meeting and hear it in your own language, live."
)

# Min Windows 10 build for the process-exclude WASAPI loopback path (2004 / 19041).
MIN_VERSION = "10.0.19041.0"
MAX_VERSION_TESTED = "10.0.26100.0"

# Required visual assets: (filename, width, height).
ASSET_SIZES = [
    ("Square44x44Logo.png", 44, 44),
    ("Square71x71Logo.png", 71, 71),
    ("Square150x150Logo.png", 150, 150),
    ("Square310x310Logo.png", 310, 310),
    ("Wide310x150Logo.png", 310, 150),
    ("StoreLogo.png", 50, 50),
    ("SplashScreen.png", 620, 300),
]


def read_version() -> str:
    """Package version as a 4-part string from app/__init__.py APP_VERSION."""
    text = (APP_DIR / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise ValueError("APP_VERSION not found in app/__init__.py")
    parts = m.group(1).split(".")
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts[:4])


def find_makeappx() -> str:
    found = shutil.which("makeappx.exe")
    if found:
        return found
    base = pathlib.Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    cands = sorted(base.glob("*/x64/makeappx.exe"), reverse=True)
    if cands:
        return str(cands[0])
    raise FileNotFoundError(
        "makeappx.exe not found. Install the Windows 10/11 SDK (App packaging tools)."
    )


def generate_assets():
    """Render every required tile/logo PNG by centering the source icon on a
    transparent canvas (square logos fill ~84%, wide/splash keep aspect)."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    src = Image.open(SRC_IMAGE).convert("RGBA")
    for name, w, h in ASSET_SIZES:
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        fill = 0.84 if w == h else 0.60          # leave padding around the glyph
        box = int(min(w, h) * fill)
        icon = src.copy()
        icon.thumbnail((box, box), Image.LANCZOS)
        canvas.paste(icon, ((w - icon.width) // 2, (h - icon.height) // 2), icon)
        canvas.save(ASSETS_DIR / name, "PNG")
    print(f"[+] Generated {len(ASSET_SIZES)} logo assets -> {ASSETS_DIR}")


def write_manifest(version: str):
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<Package
  xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  IgnorableNamespaces="uap rescap">
  <Identity Name="{IDENTITY_NAME}"
            Publisher="{PUBLISHER}"
            Version="{version}"
            ProcessorArchitecture="x64" />
  <Properties>
    <DisplayName>{APP_DISPLAY}</DisplayName>
    <PublisherDisplayName>{PUBLISHER_DISPLAY}</PublisherDisplayName>
    <Logo>Assets\\StoreLogo.png</Logo>
  </Properties>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="{MIN_VERSION}" MaxVersionTested="{MAX_VERSION_TESTED}" />
  </Dependencies>
  <Resources>
    <Resource Language="en-us" />
  </Resources>
  <Applications>
    <Application Id="Voxis" Executable="VoxisLive.exe" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements
        DisplayName="{APP_DISPLAY}"
        Description="{APP_DESCRIPTION}"
        BackgroundColor="transparent"
        Square150x150Logo="Assets\\Square150x150Logo.png"
        Square44x44Logo="Assets\\Square44x44Logo.png">
        <uap:DefaultTile Wide310x150Logo="Assets\\Wide310x150Logo.png"
                         Square310x310Logo="Assets\\Square310x310Logo.png"
                         Square71x71Logo="Assets\\Square71x71Logo.png" />
        <uap:SplashScreen Image="Assets\\SplashScreen.png" />
      </uap:VisualElements>
    </Application>
  </Applications>
  <Capabilities>
    <rescap:Capability Name="runFullTrust" />
    <DeviceCapability Name="microphone" />
  </Capabilities>
</Package>
"""
    (LAYOUT_DIR / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    print(f"[+] Wrote AppxManifest.xml (version {version})")


def stage_bundle():
    if not (SRC_BUNDLE / "VoxisLive.exe").exists():
        raise FileNotFoundError(
            f"{SRC_BUNDLE}\\VoxisLive.exe not found. Run app/build_official.py first "
            "so the MSIX includes the latest build."
        )
    if LAYOUT_DIR.exists():
        shutil.rmtree(LAYOUT_DIR)
    LAYOUT_DIR.mkdir(parents=True)
    # Copy the whole onedir bundle to the layout root (VoxisLive.exe at top).
    for item in SRC_BUNDLE.iterdir():
        dst = LAYOUT_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)
    print(f"[+] Staged bundle -> {LAYOUT_DIR}")


def pack(version: str) -> pathlib.Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_msix = OUT_DIR / f"Voxis_{version}.msix"
    makeappx = find_makeappx()
    cmd = [makeappx, "pack", "/d", str(LAYOUT_DIR), "/p", str(out_msix), "/o"]
    print(f"[*] {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    print(res.stdout)
    if res.stderr:
        print(res.stderr)
    if res.returncode != 0:
        raise RuntimeError(f"makeappx failed ({res.returncode})")
    return out_msix


def main():
    version = read_version()
    print(f"=== Voxis MSIX packager — version {version} ===")
    stage_bundle()
    generate_assets()
    write_manifest(version)
    out = pack(version)
    print(f"\n[+] MSIX created: {out}")
    print("    Upload this file on the Store submission Packages page (drag-and-drop).")
    print("    No code-signing needed — the Store re-signs it.")
    print("    To smoke-test locally, sign with a self-signed cert whose subject")
    print(f"    matches Publisher exactly: {PUBLISHER}")


if __name__ == "__main__":
    main()
