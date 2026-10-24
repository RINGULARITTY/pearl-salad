#!/bin/bash
# Copie le controleur et la table de reference depuis pearl_cloud_miner, puis construit l'image.
#   ./build.sh                                   image locale pearl-salad:dev
#   ./build.sh ghcr.io/<toi>/pearl-salad:v1.0.0 --push
#   ./build.sh --sync-only                       copie seulement (avant un commit pour la CI GitHub)
set -euo pipefail
cd "$(dirname "$0")"
SRC="${PEARL_SRC:-../pearl_cloud_miner}"
mkdir -p vendor
cp "$SRC/pearl_controller.py" vendor/pearl_controller.py
cp "$SRC/dashboard/gpus.json" vendor/gpus.json
echo "vendor/ : controleur $(grep -m1 -o 'CTL_VERSION = "[^"]*"' vendor/pearl_controller.py)"
[ "${1:-}" = "--sync-only" ] && exit 0
TAG="${1:-pearl-salad:dev}"
docker build --platform linux/amd64 --build-arg IMAGE_VERSION="${TAG##*:}" -t "$TAG" .
if [ "${2:-}" = "--push" ]; then docker push "$TAG"; fi
echo "image : $TAG"
