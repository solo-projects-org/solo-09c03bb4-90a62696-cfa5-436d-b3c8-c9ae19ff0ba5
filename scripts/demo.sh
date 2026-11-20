#!/usr/bin/env bash
# Walk through the continuation-collection demo against a running stack.
#
#   ./scripts/demo.sh            # uses http://localhost:8080 / 8081
#   ARCHIVE_URL=... SOURCE_URL=...
set -euo pipefail

ARCHIVE_URL="${ARCHIVE_URL:-http://localhost:8080}"
SOURCE_URL="${SOURCE_URL:-http://localhost:8081}"
DAY="${DAY:-2026-11-20}"

banner() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

banner "Reset the source to v1 and show its state"
curl -fsS -X POST "$SOURCE_URL/v1/admin/version" \
  -H 'content-type: application/json' -d '{"version": "v1"}' >/dev/null
curl -fsS "$SOURCE_URL/v1/admin/version" | jq .

banner "Round 1: $DAY with a two-query budget"
curl -fsS -X POST "$ARCHIVE_URL/api/collection/rounds" \
  -H 'content-type: application/json' \
  -d "{\"day\": \"$DAY\", \"max_queries\": 2}" | jq .

banner "Archive view: what is still pending?"
curl -fsS "$ARCHIVE_URL/api/days/$DAY" | jq \
  '{day, day_version, complete, pending_ranges, manual_ranges, notice_count: (.notices | length)}'

banner "Rounds 2-8: keep collecting until the round reports a terminal state"
for _ in $(seq 1 7); do
  out=$(curl -fsS -X POST "$ARCHIVE_URL/api/collection/rounds" \
    -H 'content-type: application/json' -d "{\"day\": \"$DAY\"}")
  echo "$out" | jq -c '{status, queries_used, notices_collected, pending: (.pending_ranges | length), manual: (.manual_ranges | length)}'
  status=$(echo "$out" | jq -r .status)
  case "$status" in
    collected|needs_manual_review) break ;;
  esac
done

banner "Archive view after collection"
curl -fsS "$ARCHIVE_URL/api/days/$DAY" | jq .

banner "Switch the source to v2"
curl -fsS -X POST "$SOURCE_URL/v1/admin/version" \
  -H 'content-type: application/json' -d '{"version": "v2"}' | jq .

banner "Next round notices the new version and retracts the old one"
curl -fsS -X POST "$ARCHIVE_URL/api/collection/rounds" \
  -H 'content-type: application/json' -d "{\"day\": \"$DAY\"}" | jq \
  '{status, version_reset, day_version, notices_collected, pending: (.pending_ranges | length)}'

banner "Re-collect the day under v2"
for _ in $(seq 1 10); do
  out=$(curl -fsS -X POST "$ARCHIVE_URL/api/collection/rounds" \
    -H 'content-type: application/json' -d "{\"day\": \"$DAY\"}")
  echo "$out" | jq -c '{status, queries_used, notices_collected, pending: (.pending_ranges | length), manual: (.manual_ranges | length)}'
  status=$(echo "$out" | jq -r .status)
  case "$status" in
    collected|needs_manual_review) break ;;
  esac
done

banner "Archive view on v2"
curl -fsS "$ARCHIVE_URL/api/days/$DAY" | jq \
  '{day, day_version, complete, notices: [.notices[].announcement_no], manual_ranges}'

banner "Swagger UI"
echo "archive: $ARCHIVE_URL/docs"
echo "source:  $SOURCE_URL/docs"
