@echo off
cd /d "%~dp0"
rem Developer / BYOK flavor: runs from source with your own Gemini key (no server,
rem no auth). The official SaaS installer is built by release.py (its build step
rem writes the OFFICIAL marker). Leaving VOXIS_OFFICIAL_RELEASE unset makes a source
rem run default to BYOK (see app/config._resolve_official_release).
".venv\Scripts\python.exe" main.py
pause
