#!/usr/bin/env python3
"""
VoxisLive Official Build & Release Pipeline
-------------------------------------------
Automates production compiling, source hardening, configuration provisioning,
and packaging into a distributable installer or ZIP bundle.
"""

import os
import sys
import importlib.util
import json
import shutil
import subprocess
import re
import zipfile
import pathlib
import urllib.request

# Define Paths
APP_DIR = pathlib.Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

# Make `import app.*` resolve no matter how this file is launched. Run as a plain
# script (`python app/build_official.py`), Python puts app/ — not the repo root —
# on sys.path[0], so `import app.config` in build_seed_config() raised "No module
# named 'app'". Prepending the repo root makes script- and module-mode
# (`python -m app.build_official`) invocations behave identically.
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
CONFIG_PY = APP_DIR / "config.py"
CONFIG_PY_BAK = APP_DIR / "config.py.bak"
CONFIG_JSON = ROOT_DIR / "config.json"
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"
TEMP_WEB_DIR = ROOT_DIR / "web"
SPEC_FILE = ROOT_DIR / "VoxisLive.spec"
ISS_FILE = ROOT_DIR / "installer" / "voxis.iss"
ICON_FILE = APP_DIR / "assets" / "voxis.ico"

# Microsoft VC++ 2015-2022 x64 runtime, embedded in the installer for an offline,
# silent prerequisite install (see installer/voxis.iss). Fetched here rather than
# vendored (gitignored under installer/redist). Official permalink:
# https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist
REDIST_DIR = ROOT_DIR / "installer" / "redist"
REDIST_EXE = REDIST_DIR / "vc_redist.x64.exe"
REDIST_URL = "https://aka.ms/vc14/vc_redist.x64.exe"


def _read_app_version() -> str:
    """Single source of truth for the version: app/__init__.py APP_VERSION."""
    text = (APP_DIR / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise ValueError("APP_VERSION not found in app/__init__.py")
    return m.group(1)


APP_VERSION = _read_app_version()
RELEASE_VERSION = f"v{APP_VERSION}"
OUTPUT_DIR = ROOT_DIR / "production_release" / f"VoxisLive_{RELEASE_VERSION}_Setup"


def log_phase(name: str):
    print("\n" + "=" * 60)
    print(f" PHASE: {name}")
    print("=" * 60)


def assert_clean_official_flag(file_path: pathlib.Path):
    """Verify the committed config.py is NOT hard-pinned to the official flavor.

    The build no longer mutates source: flavor is selected at runtime by the
    OFFICIAL marker placed inside the bundle (see Phase 3 and
    app/config._resolve_official_release). This assertion guards against a stray
    'IS_OFFICIAL_RELEASE: bool = True' being committed by accident, which would
    ship an open-source checkout that silently behaves as SaaS."""
    if not file_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {file_path}")

    content = file_path.read_text(encoding="utf-8")
    m = re.search(r"IS_OFFICIAL_RELEASE\s*:\s*bool\s*=\s*(.+)", content)
    if not m:
        raise ValueError(f"Could not locate IS_OFFICIAL_RELEASE in {file_path}")

    # Strip a trailing comment before comparing: the greedy (.+) capture also
    # swallows any "  # ..." note, so 'True  # default' must not slip past the guard.
    rhs = m.group(1).split("#", 1)[0].strip()
    # The committed value must be a resolver call (or False), never a hard True.
    if rhs == "True":
        raise RuntimeError(
            f"{file_path.name} has IS_OFFICIAL_RELEASE hard-pinned to True; the "
            "committed tree must stay flavor-neutral (the OFFICIAL marker selects "
            "the SaaS flavor at runtime)."
        )
    print(f"[+] Clean-tree check OK: IS_OFFICIAL_RELEASE = {rhs} in {file_path.name}")


def ensure_vc_redist() -> bool:
    """Stage installer/redist/vc_redist.x64.exe for the Inno Setup [Files] entry.

    Downloads the latest x64 redist from the official aka.ms permalink if not already
    present. Returns True if the binary is available, False otherwise (caller decides
    whether that is fatal). The download happens at BUILD time only — the installer
    itself ships the binary offline, so the Store standalone-installer rule still holds.
    """
    if REDIST_EXE.exists() and REDIST_EXE.stat().st_size > 0:
        print(f"[+] VC++ redist already staged: {REDIST_EXE}")
        return True

    REDIST_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[*] Downloading VC++ redist -> {REDIST_EXE}\n    from {REDIST_URL}")
    try:
        tmp = REDIST_EXE.with_suffix(".exe.part")
        # timeout bounds the connect and every read; without it a mid-stream CDN
        # stall would hang copyfileobj forever and wedge the whole release pipeline.
        with urllib.request.urlopen(REDIST_URL, timeout=60) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        tmp.replace(REDIST_EXE)
        print(f"[+] VC++ redist staged ({REDIST_EXE.stat().st_size} bytes)")
        return True
    except Exception as e:
        print(f"[-] Failed to fetch VC++ redist: {e}")
        print(f"    Place the file manually at {REDIST_EXE} and re-run, or download from")
        print("    https://aka.ms/vc14/vc_redist.x64.exe")
        return False


def find_iscc_compiler() -> str:
    """
    Searches for the Inno Setup compiler executable (ISCC.exe).
    """
    # Check in PATH
    iscc_path = shutil.which("ISCC.exe") or shutil.which("ISCC")
    if iscc_path:
        return iscc_path

    # Check common Windows directories
    common_paths = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
        r"C:\Program Files\Inno Setup 5\ISCC.exe",
    ]
    for path in common_paths:
        if os.path.exists(path):
            return path

    return ""


# API-key shapes that must never appear in the shipped seed's serialized text
# (sk-… = OpenAI/DashScope, AIza… = Google/Gemini). Belt-and-braces on top of the
# config.SEED_WHITELIST allowlist: if that list is ever widened to admit a
# secret-bearing key, this aborts the build instead of shipping the key.
_SEED_KEY_SHAPES = re.compile(r"sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}")
_SEED_SECRET_KEY = re.compile(r"(?i)(key|secret|token|password)")


def build_seed_config() -> dict:
    """Load the developer's root config.json and reduce it to a clean production
    seed via app.config.sanitize_seed_config (whitelist-only; see P0 #8)."""
    import app.config as appconfig  # local: avoid importing the app at module load
    with open(CONFIG_JSON, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{CONFIG_JSON} is not a JSON object")
    return appconfig.sanitize_seed_config(raw)


def assert_seed_is_clean(seed: dict) -> None:
    """Fail the build if the seed carries a secret-like key with a value or any
    API-key-shaped string. Runs on the exact dict about to be written."""
    for key, value in seed.items():
        if isinstance(value, str) and value and _SEED_SECRET_KEY.search(key):
            raise RuntimeError(
                f"seed config carries a secret-like key {key!r} with a value; "
                "widen config.SEED_WHITELIST intentionally or drop the key"
            )
    blob = json.dumps(seed, ensure_ascii=False)
    m = _SEED_KEY_SHAPES.search(blob)
    if m:
        raise RuntimeError(
            f"seed config contains an API-key-shaped value ({m.group(0)[:8]}…); "
            "refusing to ship a secret in the bundle"
        )


def main():
    print("Starting VoxisLive Production Release Build Pipeline...")
    temp_web_copied = False

    try:
        # ---------------------------------------------------------------------
        # Phase 1: Clean-Tree Flavor Check
        # ---------------------------------------------------------------------
        log_phase("1. Clean-Tree Flavor Check")

        # The committed source is never mutated to select the flavor. Flavor is a
        # property of the artifact: the OFFICIAL marker written into the bundle in
        # Phase 3 selects SaaS at runtime (see app/config._resolve_official_release).
        # Verify the committed config is flavor-neutral so we never accidentally
        # ship (or commit) a hard-pinned IS_OFFICIAL_RELEASE = True.
        assert_clean_official_flag(CONFIG_PY)

        # ---------------------------------------------------------------------
        # Phase 2: High-Performance PyInstaller Execution
        # ---------------------------------------------------------------------
        log_phase("2. High-Performance PyInstaller Execution")

        # PyInstaller command expects 'web;web' parameter relative to where it runs.
        # Since 'web' is in 'app/web', copy it temporarily to root folder to satisfy the exact argument.
        print("Temporarily staging 'web' directory to root workspace...")
        if TEMP_WEB_DIR.exists():
            shutil.rmtree(TEMP_WEB_DIR)
        shutil.copytree(APP_DIR / "web", TEMP_WEB_DIR)
        temp_web_copied = True

        # Find PyInstaller executable
        pyinstaller_cmd = None
        venv_pyinstaller = ROOT_DIR / ".venv" / "Scripts" / "pyinstaller.exe"
        if venv_pyinstaller.exists():
            pyinstaller_cmd = [str(venv_pyinstaller)]
        elif shutil.which("pyinstaller"):
            pyinstaller_cmd = ["pyinstaller"]
        else:
            # Check if PyInstaller is available as a module
            try:
                if importlib.util.find_spec("PyInstaller") is None:
                    raise ImportError
                pyinstaller_cmd = [sys.executable, "-m", "PyInstaller"]
            except ImportError:
                pass

        if not pyinstaller_cmd:
            print("[!] PyInstaller not found in environment. Falling back to system 'pyinstaller' call.")
            pyinstaller_cmd = ["pyinstaller"]

        # Formulate precise PyInstaller arguments as requested
        cmd = pyinstaller_cmd + [
            "--noconfirm",
            "--onedir",
            "--windowed",
            "--name=VoxisLive",
            "--icon", str(ICON_FILE),
            # Bundled read-only assets land in _internal/ and are resolved via app/paths.py.
            "--add-data", f"web{os.pathsep}web",
            "--add-data", f"models{os.pathsep}models",
            "--add-data", f"app{os.sep}assets{os.pathsep}assets",
            "--collect-all", "dotenv",
            "--collect-all", "webview",
            "--collect-all", "comtypes",
            "--hidden-import=webview.platforms.winforms",
            "--hidden-import=onnxruntime",
            "--clean",
            "main.py"
        ]

        print(f"Executing compilation command: {' '.join(cmd)}")
        # Flavor is selected at runtime by the OFFICIAL marker; the env var only
        # influences any build-time import that reads the flag from source (it is
        # ignored by frozen artifacts, see app/config._resolve_official_release).
        build_env = dict(os.environ, VOXIS_OFFICIAL_RELEASE="1")
        result = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, env=build_env)
        
        # Print output logs
        print("\n--- PYINSTALLER STDOUT ---")
        print(result.stdout)
        if result.stderr:
            print("\n--- PYINSTALLER STDERR ---")
            print(result.stderr)

        if result.returncode != 0:
            print("[X] PyInstaller compilation failed!")
            raise RuntimeError(f"PyInstaller compilation failed with return code {result.returncode}")
        
        print("[+] PyInstaller completed compilation successfully.")

        # ---------------------------------------------------------------------
        # Phase 3: Asset & Core Configuration Provisioning
        # ---------------------------------------------------------------------
        log_phase("3. Asset & Core Configuration Provisioning")
        
        target_dist_dir = DIST_DIR / "VoxisLive"
        if not target_dist_dir.exists():
            raise FileNotFoundError(f"Compilation output directory does not exist: {target_dist_dir}")

        # Strip onnxruntime's DirectML provider from the bundle. Voxis forces
        # CPUExecutionProvider in vad.py; DirectML.dll is never loaded but adds
        # ~18 MB to the installer. Safe to remove unconditionally.
        directml = target_dist_dir / "_internal" / "onnxruntime" / "capi" / "DirectML.dll"
        if directml.exists():
            directml.unlink()
            print(f"[+] Removed unused DirectML provider (~18 MB): {directml.name}")

        # web/ and assets/ are bundled by --add-data into _internal and resolved at
        # runtime by app/paths.py — no manual provisioning needed. We only seed the
        # production config.json next to the bundle so first run can copy it to
        # %APPDATA%\Voxis (see app/config._seed_from_bundle / app/paths).
        if not CONFIG_JSON.exists():
            raise FileNotFoundError(f"Production config.json not found in root: {CONFIG_JSON}")

        # Seed a SANITIZED config.json (whitelist-only), never the developer's
        # working file verbatim: the root config.json carries secrets (qwen_key),
        # machine-specific device names, window geometry and _pending_* state that
        # must not ship to every user via _seed_from_bundle. See P0 #8.
        dest_config_internal = target_dist_dir / "_internal" / "config.json"
        seed = build_seed_config()
        assert_seed_is_clean(seed)  # fail-closed before anything is written
        seed_text = json.dumps(seed, ensure_ascii=False, indent=2)
        print(f"Seeding sanitized config.json ({len(seed)} keys) -> "
              f"{dest_config_internal.relative_to(target_dist_dir)}")
        dest_config_internal.write_text(seed_text, encoding="utf-8")

        # Flavor marker: its presence inside the bundle selects the SaaS flavor at
        # runtime (see app/config._resolve_official_release). This is the robust
        # alternative to relying solely on the source patch above.
        marker = target_dist_dir / "_internal" / "OFFICIAL"
        print(f"Writing official flavor marker -> {marker.relative_to(target_dist_dir)}")
        marker.write_text("official\n", encoding="utf-8")

        # Version stamp: lets build_msix.py verify the frozen bundle was produced
        # from the current APP_VERSION before packaging, so it can never wrap a
        # stale engine in a newer manifest (see build_msix.stage_bundle).
        stamp = target_dist_dir / "_internal" / "BUILD_VERSION"
        stamp.write_text(APP_VERSION + "\n", encoding="utf-8")
        print(f"Writing build version stamp -> {stamp.relative_to(target_dist_dir)} ({APP_VERSION})")

        # Validate the bundled web UI is present where paths.web_dir() expects it.
        web_index_path = target_dist_dir / "_internal" / "web" / "index.html"
        print(f"Validating bundled web asset at: {web_index_path}")
        if not web_index_path.exists():
            raise FileNotFoundError(f"Critical web asset missing in bundle: {web_index_path}")

        print("[+] Asset and configuration validation successful.")

        # ---------------------------------------------------------------------
        # Phase 4: Automated Distributable Setup Installer Compilation
        # ---------------------------------------------------------------------
        log_phase("4. Distributable Setup Installer Compilation")
        
        # Ensure setup directory is created
        print(f"Ensuring output setup folder exists: {OUTPUT_DIR}")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        iscc_bin = find_iscc_compiler()
        if iscc_bin:
            print(f"[+] Inno Setup Compiler found at: {iscc_bin}")
            if not ISS_FILE.exists():
                raise FileNotFoundError(f"Installer script not found: {ISS_FILE}")

            # The installer embeds the VC++ runtime (offline, silent) so an un-updated
            # Windows 10 can load Python 3.13's python3xx.dll. Stage it before compiling.
            if not ensure_vc_redist():
                raise RuntimeError(
                    "VC++ redist could not be staged; installer would fail to compile "
                    f"the [Files] entry. Provide {REDIST_EXE} manually and re-run."
                )

            # Compile the committed installer script, injecting version/paths as defines.
            compile_cmd = [
                iscc_bin,
                f"/DMyAppVersion={APP_VERSION}",
                f"/DSourceDir={target_dist_dir}",
                f"/DOutputDir={OUTPUT_DIR}",
                str(ISS_FILE),
            ]
            print(f"Compiling Setup Installer via Inno Setup: {' '.join(compile_cmd)}")
            iscc_result = subprocess.run(compile_cmd, capture_output=True, text=True)
            print(iscc_result.stdout)
            if iscc_result.stderr:
                print(iscc_result.stderr)

            if iscc_result.returncode != 0:
                raise RuntimeError(f"ISCC compilation failed with return code {iscc_result.returncode}")

            print(f"[+] Setup installer successfully created at {OUTPUT_DIR}")
            # Distribution is Microsoft Store-only: the Store delivers updates, so
            # no self-update manifest (latest.json) is emitted. The .exe here is a
            # sideload/OSS artifact only.
        else:
            print("[-] Inno Setup Compiler (ISCC.exe) not found. Falling back to compressed ZIP compilation...")
            # The ZIP has no installer, so it cannot run the VC++ redist. Users on an
            # un-updated Windows 10 must install it manually or python3xx.dll won't load.
            print("[!] ZIP build: no VC++ redist bundled. Document that users may need")
            print("    https://aka.ms/vc14/vc_redist.x64.exe on older Windows 10.")

            output_zip_path = OUTPUT_DIR / f"VoxisLive_{RELEASE_VERSION}.zip"
            print(f"Compiling compressed ZIP bundle: {output_zip_path}")
            
            with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
                for file_path in target_dist_dir.rglob("*"):
                    if file_path.is_file():
                        arcname = file_path.relative_to(target_dist_dir)
                        zipf.write(file_path, arcname=arcname)
            
            print(f"[+] Successfully compiled ZIP archive: {output_zip_path} ({os.path.getsize(output_zip_path)} bytes)")

    except Exception as e:
        print(f"\n[X] Build Pipeline failed during execution: {e}")
        sys.exit(1)

    finally:
        # ---------------------------------------------------------------------
        # Phase 5: Self-Healing Cleanup Protocol
        # ---------------------------------------------------------------------
        log_phase("5. Self-Healing Cleanup Protocol")
        
        # Clean up temporary web directory in root folder
        if temp_web_copied and TEMP_WEB_DIR.exists():
            print("Removing temporary 'web' staging directory...")
            shutil.rmtree(TEMP_WEB_DIR, ignore_errors=True)

        # Source is never mutated, so there is nothing to restore. Re-run the
        # clean-tree assertion as a tripwire: if anything wrote a hard True into
        # the committed config during the build, fail loudly instead of shipping
        # or committing a flavor-pinned source tree.
        try:
            assert_clean_official_flag(CONFIG_PY)
        except Exception as chk_err:
            print(f"[X] Clean-tree assertion failed post-build: {chk_err}")
            raise

        # Remove any stale backup left by an older version of this pipeline.
        if CONFIG_PY_BAK.exists():
            print("Removing stale config.py backup from a previous build...")
            try:
                os.remove(CONFIG_PY_BAK)
            except OSError as bak_err:
                print(f"Warning removing stale backup: {bak_err}")

        # Clean up PyInstaller speculative spec files if generated
        if SPEC_FILE.exists():
            print("Removing temporary spec file...")
            os.remove(SPEC_FILE)

        print("\nVoxisLive Build Pipeline cleanup complete.")


if __name__ == "__main__":
    main()
