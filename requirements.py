# Runs a quick "pip install -r requirements.txt" at startup (idempotent).
# Safe to keep in production: pip skips already-satisfied deps, and you can
# disable it with BOOTSTRAP_DEPS=0.

import os, sys, subprocess

REQ_FILE = os.environ.get("REQ_FILE", "requirements.txt")
DO_BOOTSTRAP = os.environ.get("BOOTSTRAP_DEPS", "1") == "1"

def _ensure_pip():
    try:
        import pip  # noqa: F401
    except Exception:
        try:
            import ensurepip
            ensurepip.bootstrap()
        except Exception as e:
            print(f"[bootstrap] Could not bootstrap pip automatically: {e}", file=sys.stderr)
            # If this happens, asking the user to install pip manually is the fallback.
            raise

def install_from_requirements():
    if not DO_BOOTSTRAP:
        print("[bootstrap] Skipped (BOOTSTRAP_DEPS=0).")
        return
    if not os.path.exists(REQ_FILE):
        print(f"[bootstrap] {REQ_FILE} not found; skipping.")
        return

    _ensure_pip()

    print("[bootstrap] Upgrading pip…")
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=True)

    print(f"[bootstrap] Installing/Updating packages from {REQ_FILE}…")
    # --upgrade: bump versions if newer are available
    # --no-input: avoid hanging in services
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "-r", REQ_FILE, "--no-input"]
    res = subprocess.run(cmd, check=False)
    if res.returncode != 0:
        print("[bootstrap] pip install failed; see logs above.", file=sys.stderr)
        sys.exit(res.returncode)

# Run immediately on import (so it happens before other imports)
install_from_requirements()
