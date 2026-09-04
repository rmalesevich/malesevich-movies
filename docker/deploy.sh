#!/bin/sh
# Update a TrueNAS deployment: pull the latest code, rebuild the local image,
# and recreate the container so it actually runs the new build.
#
# Run this over SSH from the repo directory on the NAS, WITHOUT sudo:
#     cd /mnt/SSDPool/Application_Data/Malesevich-Movies && ./docker/deploy.sh
#
# It elevates the docker commands on its own if the daemon socket needs it.
# Running the whole script under sudo would make `git pull` run as root, which
# leaves root-owned files in the repo and trips git's "dubious ownership" check.
#
# The TrueNAS Apps "Restart" button is NOT enough after a rebuild: restarting a
# container reuses the image it was created from. The container has to be
# recreated, which is what the `compose up -d` at the end does.
set -e

IMAGE="${IMAGE:-malesevich-movies:latest}"
CONTAINER="${CONTAINER:-malesevich-movies}"

cd "$(dirname "$0")/.."

# --- how do we reach the docker daemon? ------------------------------------
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
    if command -v sudo >/dev/null 2>&1; then
        echo "==> Docker socket needs elevation, using sudo"
        DOCKER="sudo docker"
        if ! $DOCKER info >/dev/null 2>&1; then
            echo "ERROR: cannot reach the Docker daemon even with sudo." >&2
            exit 1
        fi
    else
        cat >&2 <<'MSG'
ERROR: cannot connect to the Docker daemon socket, and sudo is not available.

Either run this as a user in the "docker" group, or install/permit sudo.
MSG
        exit 1
    fi
fi

if [ "$(id -u)" = "0" ]; then
    echo "NOTE: running as root - git pull will create root-owned files."
    echo "      Prefer running this as your normal user; docker is elevated"
    echo "      automatically."
fi

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Building $IMAGE"
$DOCKER build -t "$IMAGE" .

# The app is owned by TrueNAS, so reuse the compose project and file that
# TrueNAS created rather than starting a competing project.
if ! $DOCKER inspect "$CONTAINER" >/dev/null 2>&1; then
    cat <<MSG

The container "$CONTAINER" does not exist yet, so there is nothing to recreate.
The image is built and ready. Install the app once from the TrueNAS UI:

    Apps -> Discover Apps -> Custom App -> Install via YAML

MSG
    exit 0
fi

PROJECT=$($DOCKER inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER")
CONFIG=$($DOCKER inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$CONTAINER")

if [ -z "$PROJECT" ] || [ -z "$CONFIG" ]; then
    echo "Could not read the compose labels off $CONTAINER." >&2
    echo "Recreate the app from the TrueNAS UI instead: Edit -> Save." >&2
    exit 1
fi

echo "==> Recreating container (project: $PROJECT)"
$DOCKER compose -p "$PROJECT" -f "$CONFIG" up -d

echo "==> Done. Current image:"
$DOCKER inspect -f '    {{.Config.Image}} ({{.Image}})' "$CONTAINER"
