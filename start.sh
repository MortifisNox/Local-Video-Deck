#!/usr/bin/env bash
# Local Video Deck: installs anything missing, then starts the app in your browser.
#
#   ./start.sh                 install what's missing and open the app
#   ./start.sh --install-only  install what's missing, don't start
#   ./start.sh --no-browser    start without opening a browser tab
#   ./start.sh --port 9000     use another port (default 8765)
#   ./start.sh --add-shortcut  add Local Video Deck to your app menu
#   ./start.sh --help          show this help
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
APP_DIR="$PWD"

# Official Real-ESRGAN portable build for Linux (BSD-3-Clause, by Xintao Wang).
ESRGAN_URL="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
ESRGAN_SHA256="e5aa6eb131234b87c0c51f82b89390f5e3e642b7b70f2b9bbe95b6a285a40c96"
ESRGAN_DIR="$APP_DIR/tools/realesrgan"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

OPEN=1 INSTALL_ONLY=0 SHORTCUT=0
PORT="${PORT:-8765}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-only) INSTALL_ONLY=1 ;;
    --no-browser)   OPEN=0 ;;
    --port)         PORT="${2:?--port needs a number}"; shift ;;
    --add-shortcut) SHORTCUT=1 ;;
    -h|--help)      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)              die "Unknown option: $1 (see ./start.sh --help)" ;;
  esac
  shift
done
[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port needs a number."

[[ "$(uname -s)" == Linux ]] || die "Local Video Deck runs on Linux only."
[[ "$(uname -m)" == x86_64 ]] || warn "Real-ESRGAN's Linux build is for x86_64 PCs. Enhance may not work on $(uname -m)."

# ---------- system packages ----------
has_lib() {
  local libs
  libs="$({ ldconfig -p || /sbin/ldconfig -p; } 2>/dev/null)" || true
  [[ "$libs" == *"$1"* ]]
}

PM=""
for p in dnf apt-get pacman zypper; do command -v "$p" >/dev/null && { PM="$p"; break; }; done

# Package names per package manager; "a|b" means try a, then b.
pkg_for() {
  case "$PM:$1" in
    dnf:python)      echo python3 ;;
    dnf:ffmpeg)      echo "ffmpeg|ffmpeg-free" ;;  # full ffmpeg needs RPM Fusion; ffmpeg-free is the fallback
    dnf:vulkan)      echo vulkan-loader ;;
    dnf:gomp)        echo libgomp ;;
    apt-get:python)  echo python3 ;;
    apt-get:ffmpeg)  echo ffmpeg ;;
    apt-get:vulkan)  echo libvulkan1 ;;
    apt-get:gomp)    echo libgomp1 ;;
    pacman:python)   echo python ;;
    pacman:ffmpeg)   echo ffmpeg ;;
    pacman:vulkan)   echo vulkan-icd-loader ;;
    pacman:gomp)     echo gcc-libs ;;
    zypper:python)   echo python3 ;;
    zypper:ffmpeg)   echo "ffmpeg|ffmpeg-7|ffmpeg-6" ;;
    zypper:vulkan)   echo libvulkan1 ;;
    zypper:gomp)     echo libgomp1 ;;
  esac
}
install_cmd() {
  case "$PM" in
    dnf)     echo "sudo dnf install -y" ;;
    apt-get) echo "sudo apt-get install -y" ;;
    pacman)  echo "sudo pacman -S --needed --noconfirm" ;;
    zypper)  echo "sudo zypper --non-interactive install" ;;
  esac
}

missing=()
command -v python3 >/dev/null || missing+=(python)
{ command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; } || missing+=(ffmpeg)
has_lib libvulkan.so.1 || missing+=(vulkan)
has_lib libgomp.so.1 || missing+=(gomp)

if [[ ${#missing[@]} -gt 0 ]]; then
  declare -A label=([python]="Python 3" [ffmpeg]="ffmpeg" [vulkan]="the Vulkan loader" [gomp]="the OpenMP runtime (libgomp)")
  names=(); for m in "${missing[@]}"; do names+=("${label[$m]}"); done
  say "Local Video Deck needs: $(IFS=,; echo "${names[*]}" | sed 's/,/, /g')"
  [[ -n "$PM" ]] || die "Install them with your system's package manager, then run ./start.sh again."
  if [[ -t 0 ]]; then
    read -r -p "Install them now with $PM? This asks for your password. [Y/n] " answer
    [[ -z "$answer" || "$answer" =~ ^[Yy] ]] || die "Install them yourself, then run ./start.sh again."
  else
    die "Run ./start.sh in a terminal so it can install them, or install them yourself."
  fi
  [[ "$PM" == apt-get ]] && sudo apt-get update
  for m in "${missing[@]}"; do
    ok=0
    IFS='|' read -r -a choices <<< "$(pkg_for "$m")"
    for pkg in "${choices[@]}"; do
      say "Installing $pkg…"
      # shellcheck disable=SC2046
      if $(install_cmd) "$pkg"; then ok=1; break; fi
    done
    [[ $ok == 1 ]] || die "Couldn't install ${label[$m]}. Install it yourself, then run ./start.sh again."
  done
  if [[ "$PM" == dnf ]] && rpm -q ffmpeg-free >/dev/null 2>&1; then
    warn "Installed ffmpeg-free. For faster, higher-quality H.264 and H.265, enable RPM Fusion and run: sudo dnf swap ffmpeg-free ffmpeg --allowerasing"
  fi
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "Local Video Deck needs Python 3.8 or newer."

# ---------- Real-ESRGAN ----------
if [[ ! -x "$ESRGAN_DIR/realesrgan-ncnn-vulkan" ]]; then
  say "Downloading Real-ESRGAN (47 MB)…"
  python3 -I - "$ESRGAN_URL" "$ESRGAN_SHA256" "$ESRGAN_DIR" <<'PY'
import hashlib, os, shutil, sys, tempfile, urllib.request, zipfile

url, want_sha, dest = sys.argv[1:4]
tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
try:
    with urllib.request.urlopen(url, timeout=60) as r, tmp_zip:
        total, done, sha = int(r.headers.get("Content-Length") or 0), 0, hashlib.sha256()
        while chunk := r.read(1 << 16):
            tmp_zip.write(chunk); sha.update(chunk); done += len(chunk)
            if total:
                print(f"\r  {done * 100 // total}%", end="", flush=True)
    print()
    if sha.hexdigest() != want_sha:
        sys.exit("The download doesn't match the expected checksum, so it wasn't installed. Try again later.")
    staging = dest + ".partial"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(tmp_zip.name) as z:
        # Only the program, its models and its readme; never paths outside the folder.
        for name in z.namelist():
            if name == "realesrgan-ncnn-vulkan" or name == "README_ubuntu.md" or (
                    name.startswith("models/") and ".." not in name and not name.endswith("/")):
                z.extract(name, staging)
    os.chmod(os.path.join(staging, "realesrgan-ncnn-vulkan"), 0o755)
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.rename(staging, dest)
except OSError as e:
    sys.exit(f"Couldn't download Real-ESRGAN: {e}. Check your internet connection and run ./start.sh again.")
finally:
    os.unlink(tmp_zip.name)
PY
  say "Real-ESRGAN installed."
fi

# ---------- app menu shortcut ----------
if [[ $SHORTCUT == 1 ]]; then
  apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$apps"
  cat > "$apps/local-video-deck.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Local Video Deck
Comment=Compare, convert and enhance videos
Exec="$APP_DIR/start.sh"
Icon=$APP_DIR/web/icon.svg
Terminal=true
Categories=AudioVideo;Video;
EOF
  say "Added Local Video Deck to your app menu."
  exit 0
fi

if [[ $INSTALL_ONLY == 1 ]]; then
  say "Everything is installed. Run ./start.sh to open Local Video Deck."
  exit 0
fi

export PORT
if [[ $OPEN == 1 ]]; then exec python3 server.py --open; else exec python3 server.py; fi
