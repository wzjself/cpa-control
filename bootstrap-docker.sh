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
  apt-get install -y curl ca-certificates docker.io docker-compose-plugin tar
}

install_pkgs_yum() {
  yum install -y curl ca-certificates docker tar
}

install_pkgs_dnf() {
  dnf install -y curl ca-certificates docker docker-compose tar
}

install_pkgs_apk() {
  apk add --no-cache curl ca-certificates docker-cli-compose docker tar
}

ensure_system_deps() {
  if need_cmd docker && need_cmd curl && need_cmd tar; then
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
    echo "Unsupported system package manager. Please install manually: docker curl tar" >&2
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

cp -a "$SRC_DIR"/. "$INSTALL_DIR"/
cd "$INSTALL_DIR"
mkdir -p data
chmod +x bootstrap-docker.sh || true

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now docker >/dev/null 2>&1 || true
fi

if docker compose version >/dev/null 2>&1; then
  docker compose up -d --build
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose up -d --build
else
  echo "Docker Compose is not available" >&2
  exit 1
fi

cat <<EOF

CPA Control installed in Docker successfully.

Install dir:
  $INSTALL_DIR

Open:
  http://127.0.0.1:${CPA_CONTROL_PORT:-8321}

View logs:
  cd $INSTALL_DIR && docker compose logs -f

Restart:
  cd $INSTALL_DIR && docker compose up -d --build
EOF
