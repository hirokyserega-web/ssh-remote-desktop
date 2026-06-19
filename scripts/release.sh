#!/usr/bin/env bash
# Maintainer helper: cut a release.
#
#   scripts/release.sh 1.1.0
#
# What it does:
#   1. verifies the working tree is clean,
#   2. validates the version string (semver X.Y.Z),
#   3. updates VERSION and prepends an entry to CHANGELOG.md,
#   4. commits the bump,
#   5. creates an annotated tag vX.Y.Z,
#   6. pushes the current branch + the tag (which triggers release.yml).
#
# Idempotent: re-running with the same version on an already-tagged commit is a
# no-op (it detects the existing tag and exits 0). It never force-pushes and
# never overwrites an existing tag.
set -euo pipefail

VERSION_FILE="VERSION"
CHANGELOG="CHANGELOG.md"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-main}"

die() { printf '\033[1;31mERR:\033[0m %s\n' "$*" >&2; exit 1; }
log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# ---- arg parsing -----------------------------------------------------------
VERSION=""
PUSH="yes"
DRY_RUN="no"
for arg in "$@"; do
  case "$arg" in
    --no-push) PUSH="no";;
    --dry-run) DRY_RUN="yes"; PUSH="no";;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*) die "unknown flag: $arg";;
    *) [[ -z "$VERSION" ]] || die "only one version argument allowed"; VERSION="$arg";;
  esac
done
[[ -n "$VERSION" ]] || die "usage: scripts/release.sh X.Y.Z [--no-push] [--dry-run]"

# ---- normalize a leading-v input (v1.2.3 -> 1.2.3) -------------------------
VERSION="${VERSION#v}"

# ---- validate semver -------------------------------------------------------
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  die "version must look like X.Y.Z (got '$VERSION')"
fi
TAG="v${VERSION}"

# ---- preconditions ---------------------------------------------------------
cd "$(git rev-parse --show-toplevel)"
log "Working directory: $(pwd)"

if [[ -n "$(git status --porcelain)" ]]; then
  die "working tree is not clean; commit or stash first"
fi
if ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  die "branch '$BRANCH' not found"
fi
if git rev-parse --verify "refs/tags/$TAG" >/dev/null 2>&1; then
  log "tag $TAG already exists — nothing to do."
  exit 0
fi

CURRENT="$(tr -d '[:space:]' < "$VERSION_FILE")"
if [[ "$CURRENT" == "$VERSION" ]]; then
  log "VERSION already at $VERSION"
else
  log "Bumping VERSION: $CURRENT -> $VERSION"
  printf '%s\n' "$VERSION" > "$VERSION_FILE"
  # Keep pyproject.toml in sync.
  if [[ -f pyproject.toml ]] && grep -q '^version = "' pyproject.toml; then
    sed -i "s/^version = \".*\"/version = \"$VERSION\"/" pyproject.toml
  fi
fi

# ---- changelog -------------------------------------------------------------
DATE="$(date +%Y-%m-%d)"
if [[ -f "$CHANGELOG" ]] && grep -q "^## \[Unreleased\]" "$CHANGELOG"; then
  # Promote the Unreleased block to the new version.
  awk -v v="$VERSION" -v d="$DATE" '
    /^## \[Unreleased\]/ { print; print ""; print "## ['"$VERSION"'] - '"$DATE"'"; next }
    { print }
  ' "$CHANGELOG" > "$CHANGELOG.tmp" && mv "$CHANGELOG.tmp" "$CHANGELOG"
elif [[ -f "$CHANGELOG" ]] && grep -q "^## \[Unreleased\]" "$CHANGELOG" 2>/dev/null; then
  :
else
  log "CHANGELOG has no [Unreleased] section; leaving it as-is."
fi

# ---- commit + tag ----------------------------------------------------------
git add "$VERSION_FILE" pyproject.toml "$CHANGELOG" 2>/dev/null || git add "$VERSION_FILE" "$CHANGELOG"
if git diff --cached --quiet; then
  log "No changes to commit (VERSION + CHANGELOG already up to date)."
else
  log "Committing release bump"
  if [[ "$DRY_RUN" == "yes" ]]; then
    log "[dry-run] would run: git commit -m \"release: v${VERSION}\""
    git reset -q HEAD -- "$VERSION_FILE" pyproject.toml "$CHANGELOG" 2>/dev/null || true
  else
    git commit -m "release: v${VERSION}" >/dev/null
  fi
fi

log "Creating annotated tag $TAG"
if [[ "$DRY_RUN" == "yes" ]]; then
  log "[dry-run] would run: git tag -a \"$TAG\" -m \"Release v${VERSION}\""
else
  git tag -a "$TAG" -m "Release v${VERSION}"
fi

if [[ "$DRY_RUN" == "yes" ]]; then
  log "[dry-run] would push branch $BRANCH and tag $TAG to $REMOTE"
elif [[ "$PUSH" == "yes" ]]; then
  log "Pushing branch $BRANCH and tag $TAG to $REMOTE"
  git push "$REMOTE" "$BRANCH"
  git push "$REMOTE" "$TAG"
else
  log "--no-push: tag created locally. Push with: git push $REMOTE $BRANCH $TAG"
fi

log "Done. The Release workflow will build and publish binaries for $TAG."
