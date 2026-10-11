#!/usr/bin/env bash
# lead-bot/deploy/build-image.sh
# Builds the image for a git commit and tags it lead-bot:<short sha>.
#
#   bash deploy/build-image.sh [git-ref]        (default HEAD)
#
# The image is made from `git archive`, i.e. exactly what is committed. A plain `docker build .`
# would copy whatever is in the folder, including a half-finished edit or a stray file, and the tag
# would then name a commit the image doesn't match. Prints the tag on the last line.
set -eu

# C:/... form on Windows: git and docker are native programs and can't use /c/... paths.
REPO="$(cd "$(dirname "$0")/.." && { pwd -W 2>/dev/null || pwd; })"
ref="${1:-HEAD}"

sha="$(git -C "$REPO" rev-parse --short=10 "$ref^{commit}")" || { echo "unknown git ref: $ref" >&2; exit 1; }
tag="lead-bot:$sha"

git -C "$REPO" archive --format=tar "$sha" | docker build -q -t "$tag" - >/dev/null
echo "built $tag from commit $sha"
echo "$sha"
