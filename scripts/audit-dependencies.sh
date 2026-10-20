#!/bin/sh
# Checks the locked packages against the published advisories with pip-audit
# (pip install pip-audit==2.9.0). Advisories that do not apply to Speakr are
# listed in scripts/audit-exceptions.txt with a review date and a reason; an
# exception past its review date no longer applies, so the advisory fails the
# check again until it is looked at.
#
#   scripts/audit-dependencies.sh
set -eu
cd "$(dirname "$0")/.."

today=$(date -u +%Y-%m-%d)
ignores=""
while read -r id until reason; do
    case "$id" in ''|'#'*) continue ;; esac
    first=$(printf '%s\n%s\n' "$today" "$until" | sort | head -n 1)
    if [ "$until" = never ] || [ "$first" = "$today" ]; then
        ignores="$ignores --ignore-vuln $id"
    else
        echo "Exception for $id expired on $until ($reason)" >&2
    fi
done < scripts/audit-exceptions.txt

status=0
for lock in requirements.lock requirements-embeddings.lock; do
    echo "== $lock"
    # shellcheck disable=SC2086
    pip-audit --disable-pip --require-hashes --progress-spinner off -r "$lock" $ignores || status=1
done
exit $status
