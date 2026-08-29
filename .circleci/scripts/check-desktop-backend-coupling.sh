#!/usr/bin/env bash
# Fails any branch that changes both products/desktop/** and backend code that
# ships in production. Desktop releases auto-update users on their own
# schedule, with no orchestration against backend deploys, so a coupled change
# can ship a client that calls endpoints that are not deployed yet.
#
# Same policy as .github/scripts/desktop/check-pr-backend-coupling.sh. That
# script reads the changed-file list from the GitHub pull-request API and needs
# REPOSITORY, PR_NUMBER and a token; this one reads it from git, which is all
# CircleCI has. The path rules below are kept identical to it on purpose — if
# you change one, change both.
set -euo pipefail

BASE_REF="${BASE_REF:-origin/master}"

# Deploy-relevant subset of ci-backend.yml's `backend` filter (paths that ship
# in the Django image) plus rust/** (rust services deploy separately and also
# serve clients). Tooling and test entries are deliberately excluded so they
# cannot block an unrelated desktop change.
is_backend_path() {
    case "$1" in
    *.md | *.mdx) return 1 ;;
    products/desktop/*) return 1 ;;
    posthog/*) return 0 ;;
    rust/*) return 0 ;;
    ee/frontend/*) return 1 ;;
    ee/*) return 0 ;;
    common/__init__.py | common/hogql_parser/* | common/hogvm/* | common/ingestion/* | common/migration_utils/* | common/plugin_transpiler/*) return 0 ;;
    products/*/backend/* | products/*.py) return 0 ;;
    frontend/src/products.json) return 0 ;;
    *) return 1 ;;
    esac
}

is_desktop_path() {
    case "$1" in
    products/desktop/*) return 0 ;;
    *) return 1 ;;
    esac
}

# CHANGED_FILES_FILE lets a test drive the classification with a fixed list.
# Unset — which is the CI path — the list comes from git.
if [ -n "${CHANGED_FILES_FILE:-}" ]; then
    changed=$(cat "$CHANGED_FILES_FILE")
else
    merge_base=$(git merge-base "$BASE_REF" HEAD)
    changed=$(git diff --name-only "$merge_base..HEAD")
fi

desktop_hits=""
backend_hits=""
while IFS= read -r path || [ -n "$path" ]; do
    [ -n "$path" ] || continue
    if is_desktop_path "$path"; then
        desktop_hits="${desktop_hits}  ${path}"$'\n'
    elif is_backend_path "$path"; then
        backend_hits="${backend_hits}  ${path}"$'\n'
    fi
done <<EOF
$changed
EOF

if [ -n "$desktop_hits" ] && [ -n "$backend_hits" ]; then
    echo "This branch changes both the desktop app and deploy-relevant backend code."
    echo "Split it: desktop auto-updates on its own schedule, so a coupled change can"
    echo "ship a client that calls endpoints that are not deployed yet."
    echo
    echo "Desktop files:"
    printf '%s' "$desktop_hits"
    echo "Backend files:"
    printf '%s' "$backend_hits"
    exit 1
fi

echo "No desktop/backend coupling in this branch."
