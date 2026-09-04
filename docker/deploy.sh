#!/bin/sh
# Update a TrueNAS deployment: pull the latest code, rebuild the local image,
# and recreate the container so it actually runs the new build.
#
# Run this over SSH from the repo directory on the NAS:
#     cd /mnt/SSDPool/Application_Data/Malesevich-Movies && ./docker/deploy.sh
#
# The TrueNAS Apps "Restart" button is NOT enough after a rebuild: restarting a
# container reuses the image it was created from. The container has to be
# recreated, which is what the `compose up -d` at the end does.
set -e

IMAGE="${IMAGE:-malesevich-movies:latest}"
CONTAINER="${CONTAINER:-malesevich-movies}"

cd "$(dirname "$0")/.."

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Building $IMAGE"
docker build -t "$IMAGE" .

# The app is owned by TrueNAS, so reuse the compose project and file that
# TrueNAS created rather than starting a competing project.
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    cat <<MSG

The container "$CONTAINER" does not exist yet, so there is nothing to recreate.
The image is built and ready. Install the app once from the TrueNAS UI:

    Apps -> Discover Apps -> Custom App -> Install via YAML

MSG
    exit 0
fi

PROJECT=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER")
CONFIG=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$CONTAINER")

if [ -z "$PROJECT" ] || [ -z "$CONFIG" ]; then
    echo "Could not read the compose labels off $CONTAINER." >&2
    echo "Recreate the app from the TrueNAS UI instead: Edit -> Save." >&2
    exit 1
fi

echo "==> Recreating container (project: $PROJECT)"
docker compose -p "$PROJECT" -f "$CONFIG" up -d

echo "==> Done. Current image:"
docker inspect -f '    {{.Config.Image}} ({{.Image}})' "$CONTAINER"
