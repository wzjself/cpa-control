#!/usr/bin/env bash
set -euo pipefail

REPO_TARBALL_URL="https://codeload.github.com/wzjself/cpa-control/tar.gz/refs/heads/main"
INSTALL_DIR="${CPA_CONTROL_DIR:-/opt/cpa-control}"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

install_pkgs_apt() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl ca-certificates python3 python3-venv python3-pip tar
}

install_pkgs_yum() {
  yum install -y curl ca-certificates python3 python3-pip tar
  python3 -m ensurepip --upgrade >/dev/null 2>&1 || true
}

install_pkgs_dnf() {
  dnf install -y curl ca-certificates python3 python3-pip tar
  python3 -m ensurepip --upgrade >/dev/null 2>&1 || true
}

install_pkgs_apk() {
  apk add --no-cache curl ca-certificates python3 py3-pip py3-virtualenv tar
}

ensure_system_deps() {
  if need_cmd python3 && need_cmd curl && need_cmd tar; then
    return 0
  fi

  if need_cmd apt-get; then
    install_pkgs_apt
  elif need_cmd dnf; then
    install_pkgs_dnf
  elif need_cmd yum; then
    install_pkgs_yum
  elif need_cmd apk; then
    install_pkgs_apk
  else
    echo "Unsupported system package manager. Please install manually: curl python3 python3-venv tar" >&2
    exit 1
  fi
}

ensure_system_deps

mkdir -p "$INSTALL_DIR"

curl -L "$REPO_TARBALL_URL" -o "$TMP_DIR/cpa-control.tar.gz"
tar -xzf "$TMP_DIR/cpa-control.tar.gz" -C "$TMP_DIR"
SRC_DIR="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'cpa-control-*' | head -n1)"

if [ -z "$SRC_DIR" ] || [ ! -d "$SRC_DIR" ]; then
  echo "Failed to unpack source" >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"
cp -a "$SRC_DIR"/. "$INSTALL_DIR"/
cd "$INSTALL_DIR"

python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt
mkdir -p data
chmod +x install.sh bootstrap.sh || true

cat <<EOF

CPA Control installed successfully.

Install dir:
  $INSTALL_DIR

Start manually:
  cd $INSTALL_DIR && ./.venv/bin/python app.py

Or run in background:
  cd $INSTALL_DIR && nohup ./.venv/bin/python app.py > cpa-control.log 2>&1 &

Default port:
  8321

If needed, override install dir:
  CPA_CONTROL_DIR=/your/path bash bootstrap.sh
EOF
