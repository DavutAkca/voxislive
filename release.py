#!/usr/bin/env python3
"""
Voxis Release Automation
Kullanim: python release.py

Dagitim Microsoft Store-only: guncellemeler Store tarafindan dagitilir, bu yuzden
bu akis self-update manifesti (latest.json) uretmez ve sunucuya yukleme yapmaz.
Uretilen .exe yalnizca sideload/OSS artefakti olarak cikti klasorunde kalir.
"""
from __future__ import annotations
import json, os, pathlib, re, shutil, subprocess, sys, threading, time

ROOT      = pathlib.Path(__file__).resolve().parent
APP_INIT  = ROOT / "app" / "__init__.py"
BUILD_PY  = ROOT / "app" / "build_official.py"
HYGIENE   = ROOT / "scripts" / "check_release_hygiene.py"
SIGN_DIR  = pathlib.Path.home() / ".voxis-signing"
PREFS     = SIGN_DIR / "prefs.json"
PYTHON    = sys.executable

os.system("")  # Windows ANSI etkinlestir

R  = "\033[0m";  BD = "\033[1m";  DM = "\033[2m"
RD = "\033[91m"; GN = "\033[92m"; YW = "\033[93m"
BL = "\033[94m"; MG = "\033[95m"; CY = "\033[96m"; WH = "\033[97m"

ISCC_PATHS = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
    r"C:\Program Files\Inno Setup 5\ISCC.exe",
]
SIGNTOOL_DIRS = [
    r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64",
    r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64",
    r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.19041.0\x64",
    r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.17763.0\x64",
    r"C:\Program Files (x86)\Windows Kits\10\bin",
]

# ── Cikti yardimcilari ────────────────────────────────────────────────────────

def ok(m):   print(f"  {GN}+{R}  {m}")
def err(m):  print(f"  {RD}x{R}  {m}")
def warn(m): print(f"  {YW}!{R}  {m}")
def info(m): print(f"  {BL}>{R}  {m}")

def wait_key():
    """Panelin hata/bitiste kapanmamasi icin tusa basilmasini bekle."""
    try:
        input(f"\n  {DM}Kapatmak icin Enter'a bas...{R}  ")
    except (EOFError, KeyboardInterrupt):
        pass

def abort(m):
    print(f"\n{RD}{BD}  HATA: {m}{R}\n")
    wait_key()
    sys.exit(1)

def section(title):
    print(f"\n{DM}{'─' * 56}{R}")
    print(f"{BD}{WH}  {title}{R}")

def phase(n, total, title, sub=""):
    print(f"\n{BD}{CY}  [{n}/{total}]{R}  {BD}{WH}{title}{R}")
    if sub:
        print(f"         {DM}{sub}{R}")

def ask(prompt, default=""):
    hint = f"  {DM}[{default}]{R}" if default else ""
    val = input(f"\n  {CY}?{R}  {BD}{prompt}{R}{hint}:  ").strip()
    return val if val else default

def ask_yn(prompt, default=True):
    opts = f"{BD}E{R}/h" if default else f"e/{BD}H{R}"
    raw = input(f"\n  {CY}?{R}  {BD}{prompt}{R}  ({opts}):  ").strip().lower()
    if not raw:
        return default
    return raw in ("e", "evet", "y", "yes", "1")


# ── Spinner ────────────────────────────────────────────────────────────────────

class Spinner:
    _F = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    def __init__(self, msg): self.msg = msg; self._ok = True
    def fail(self): self._ok = False
    def __enter__(self):
        self._t0 = time.time(); self._run = True
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start(); return self
    def __exit__(self, *_):
        self._run = False; self._th.join()
        elapsed = time.time() - self._t0
        icon = f"{GN}+{R}" if self._ok else f"{RD}x{R}"
        print(f"\r  {icon}  {self.msg}  {DM}({elapsed:.0f}s){R}          ")
    def _loop(self):
        i = 0
        while self._run:
            e = time.time() - self._t0
            print(f"\r  {CY}{self._F[i % 10]}{R}  {self.msg}  {DM}({e:.0f}s){R}", end="", flush=True)
            i += 1; time.sleep(0.1)


# ── Arac bulucu ────────────────────────────────────────────────────────────────

def find_exe(name, extra_dirs=None):
    found = shutil.which(name)
    if found:
        return found
    for d in (extra_dirs or []):
        p = pathlib.Path(d)
        if p.is_file():          # tam yol verilmis (ornek: ...ISCC.exe)
            return str(p)
        candidate = p / name     # dizin verilmis, ust adini ekle
        if candidate.exists():
            return str(candidate)
    return ""


# ── On kontroller ──────────────────────────────────────────────────────────────

def check_prerequisites():
    section("On Kontroller")
    t = {}

    # Inno Setup
    iscc = find_exe("ISCC.exe", ISCC_PATHS) or find_exe("ISCC")
    if iscc:
        ok(f"Inno Setup bulundu")
        t["iscc"] = iscc
    else:
        warn("Inno Setup (ISCC.exe) bulunamadi -> ZIP bundle uretilecek (installer YOK)")
        t["iscc"] = ""

    # signtool
    st = find_exe("signtool.exe", SIGNTOOL_DIRS) or find_exe("signtool")
    if st:
        ok("signtool.exe bulundu")
        t["signtool"] = st
    else:
        warn("signtool.exe bulunamadi -> Authenticode imzasi atlanacak")
        t["signtool"] = ""

    # git
    g = shutil.which("git") or ""
    if g:
        ok("git mevcut")
        t["git"] = g
    else:
        warn("git bulunamadi -> commit adimi atlanacak")
        t["git"] = ""

    return t


# ── Surum yardimcilari ─────────────────────────────────────────────────────────

def read_version():
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                  APP_INIT.read_text(encoding="utf-8"))
    return m.group(1) if m else "0.0.0"

def write_version(v):
    txt = APP_INIT.read_text(encoding="utf-8")
    new = re.sub(r'(APP_VERSION\s*=\s*)["\'][^"\']+["\']', f'\\1"{v}"', txt)
    APP_INIT.write_text(new, encoding="utf-8")

def parse_ver(v):
    s = v.strip().lstrip("vV")
    parts = s.split(".")
    if len(parts) != 3: return None
    try: return tuple(int(p) for p in parts)
    except ValueError: return None

def bump_patch(v):
    p = parse_ver(v)
    return f"{p[0]}.{p[1]}.{p[2] + 1}" if p else ""


# ── Tercih hafizasi (son girilen degerler) ─────────────────────────────────────

def load_prefs():
    try:
        return json.loads(PREFS.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_prefs(p):
    try:
        SIGN_DIR.mkdir(parents=True, exist_ok=True)
        PREFS.write_text(json.dumps(p, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Faz uygulayicilari ─────────────────────────────────────────────────────────

def run_hygiene():
    r = subprocess.run([PYTHON, str(HYGIENE)], capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(f"\n{RD}{r.stdout}\n{r.stderr}{R}")
    return r.returncode == 0


def run_build(version):
    r = subprocess.run(
        [PYTHON, str(BUILD_PY)],
        capture_output=True, text=True, cwd=ROOT,
        env=dict(os.environ, VOXIS_OFFICIAL_RELEASE="1"),
    )
    if r.returncode != 0:
        tail = (r.stdout + r.stderr)[-4000:]
        print(f"\n{RD}{tail}{R}")
        return None
    out = ROOT / "production_release" / f"VoxisLive_v{version}_Setup"
    if out.exists():
        return out
    # fallback: arayi bul
    base = ROOT / "production_release"
    if base.exists():
        for p in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_dir() and version in p.name:
                return p
    return None


def sign_exe(exe_path, signtool):
    r = subprocess.run([
        signtool, "sign", "/fd", "sha256",
        "/n", "Voxis",
        "/tr", "http://timestamp.digicert.com",
        "/td", "sha256",
        str(exe_path),
    ], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"\n{YW}{r.stdout}\n{r.stderr}{R}")
    return r.returncode == 0


def git_commit(version):
    cmds = [
        ["git", "add", "app/__init__.py"],
        ["git", "commit", "-m", f"chore: bump version to {version}"],
        ["git", "push", "origin", "main"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            err(f"{' '.join(cmd[1:])} basarisiz: {r.stderr.strip()}")
            return False
    return True


def open_folder(path):
    try:
        subprocess.Popen(["explorer", str(path)],
                         creationflags=subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
    except Exception:
        pass


# ── Ana akis ───────────────────────────────────────────────────────────────────

def main():
    print(f"""
{BD}{CY}  ╔══════════════════════════════════════╗
  ║    VOXIS  RELEASE  AUTOMATION  v3    ║
  ╚══════════════════════════════════════╝{R}
  {DM}Microsoft Store-only — self-update manifesti uretilmez{R}
""")

    # ── 0. On kontroller ──────────────────────────────────────────────────────
    tools = check_prerequisites()

    if not tools["iscc"]:
        print(f"\n  {YW}Inno Setup kurulu degil!{R}")
        print(f"  {DM}installer uretmek icin: https://jrsoftware.org/isdl.php{R}")
        if ask_yn("Inno Setup olmadan devam edilsin mi? (sadece ZIP uretilir)", default=False):
            pass
        else:
            abort("Inno Setup kur ve tekrar calistir.")

    # ── 1. Bilgi toplama ──────────────────────────────────────────────────────
    section("Surum Bilgileri")
    prefs   = load_prefs()
    current = read_version()
    info(f"Mevcut surum: {WH}{BD}{current}{R}")

    while True:
        version = ask("Yeni surum numarasi (ornek: 1.0.1)", bump_patch(current))
        pv = parse_ver(version)
        pc = parse_ver(current)
        if pv is None:
            err("Gecersiz format. Ornek: 1.0.1")
        elif pc and pv <= pc:
            err(f"Yeni surum {current} surumuyle esit veya kucuk olamaz")
        else:
            break

    do_git = bool(tools["git"]) and ask_yn("Git commit + push yapilsin mi?", default=prefs.get("git", True))

    # Secimleri sonraki calistirma icin hatirla (Enter ile gelecekte kabul edilir)
    save_prefs({"git": do_git})

    # Ozet onay
    section("Ozet")
    print(f"""
  {DM}Mevcut{R}  {WH}{current}{R}
  {DM}Yeni{R}    {GN}{BD}{version}{R}

  Git commit   : {'Evet' if do_git else 'Hayir'}
""")
    if not ask_yn("Baslayalim mi?", default=True):
        print(f"\n  Iptal edildi.\n"); sys.exit(0)

    # ── Fazlar ────────────────────────────────────────────────────────────────
    TOTAL = 5
    results = {}
    prev_version = current  # geri alma icin

    # [1] Versiyon
    phase(1, TOTAL, "Versiyon Guncelleniyor", f"app/__init__.py: {current} -> {version}")
    write_version(version)
    ok(f'APP_VERSION = "{version}"')
    results["Versiyon"] = "OK"

    # [2] Temizlik
    phase(2, TOTAL, "Temizlik Kontrolu", "Siz ve closed-core sizintisi taraniyor")
    with Spinner("check_release_hygiene.py") as sp:
        hyg_ok = run_hygiene()
        if not hyg_ok: sp.fail()
    if hyg_ok:
        ok("Temiz — sizinti yok")
        results["Hygiene"] = "OK"
    else:
        err("Temizlik kontrolu basarisiz!")
        write_version(prev_version)
        warn(f"Surum {prev_version} olarak geri alindi")
        abort("Ihalleri duzelt ve tekrar calistir.")

    # [3] Build
    phase(3, TOTAL, "Build", "PyInstaller + Inno Setup (birkaç dakika sürebilir)")
    with Spinner("app/build_official.py") as sp:
        out_dir = run_build(version)
        if not out_dir: sp.fail()
    if out_dir:
        ok(f"Cikti klasoru: {out_dir.name}")
        results["Build"] = "OK"
    else:
        write_version(prev_version)
        abort("Build basarisiz. app/build_official.py manuel calistirip loglara bak.")

    # Exe yolunu bul
    exe_path = out_dir / f"VoxisLive_v{version}_Setup.exe"
    if not exe_path.exists():
        zips = list(out_dir.glob("*.zip"))
        if zips:
            warn(f".exe bulunamadi, ZIP bulundu: {zips[0].name}")
            exe_path = zips[0]
        else:
            write_version(prev_version)
            abort(f"Ne .exe ne .zip bulunamadi: {out_dir}")

    # [4] Authenticode
    phase(4, TOTAL, "Authenticode Imzasi", exe_path.name)
    if tools["signtool"] and exe_path.suffix == ".exe":
        with Spinner("signtool.exe") as sp:
            auth_ok = sign_exe(exe_path, tools["signtool"])
            if not auth_ok: sp.fail()
        if auth_ok:
            ok("Authenticode imzasi tamam")
            results["Authenticode"] = "OK"
        else:
            warn("Authenticode imzasi basarisiz (sertifika tanimli degil olabilir)")
            results["Authenticode"] = "! (hata)"
    else:
        reason = "signtool yok" if not tools["signtool"] else "ZIP bundle"
        warn(f"Authenticode atlandi ({reason})")
        results["Authenticode"] = "ATLANMADI"

    # [5] Git
    phase(5, TOTAL, "Git Commit", f"chore: bump version to {version}")
    if do_git:
        with Spinner("git add + commit + push") as sp:
            g_ok = git_commit(version)
            if not g_ok: sp.fail()
        if g_ok:
            ok("main branch'e push edildi")
            results["Git"] = "OK"
        else:
            warn("Git islemi basarisiz — manuel olarak commit at")
            results["Git"] = "! (hata)"
    else:
        info("Git atlandirildi")
        results["Git"] = "ATLANMADI"

    # ── Sonuc ─────────────────────────────────────────────────────────────────
    section("TAMAMLANDI")
    print(f"\n  {GN}{BD}Voxis {version} hazir!{R}\n")

    for label, val in results.items():
        if val == "OK":
            icon = f"{GN}+{R}"
        elif "hata" in val or "!" in val:
            icon = f"{YW}!{R}"
        elif val == "ATLANMADI":
            icon = f"{DM}-{R}"
        else:
            icon = f"{BL}>{R}"
        print(f"  {icon}  {BD}{label:<16}{R}  {DM}{val}{R}")

    print(f"\n  {BL}Cikti klasoru:{R}")
    print(f"  {WH}{BD}{out_dir}{R}\n")

    print(f"  {DM}Dagitim Store uzerinden: MSIX paketini app/build_msix.py ile{R}")
    print(f"  {DM}uretip Partner Center'a yukle. Bu .exe sideload/OSS artefaktidir.{R}\n")

    if not do_git:
        print(f"  {YW}Git commit yapilmadi.{R}")
        print(f"  {DM}git add app/__init__.py && git commit -m \"chore: bump version to {version}\" && git push{R}\n")

    open_folder(out_dir)
    print(f"  {DM}Klasor Explorer'da acildi.{R}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YW}Kullanici tarafindan iptal edildi.{R}\n")
        wait_key()
        sys.exit(0)
    except SystemExit:
        # abort() zaten mesaj + wait_key calistirdi
        raise
    except Exception:
        import traceback
        print(f"\n{RD}{BD}  BEKLENMEYEN HATA{R}\n")
        traceback.print_exc()
        wait_key()
        sys.exit(1)
    else:
        wait_key()
