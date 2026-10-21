#!/bin/bash
set -euo pipefail
ready=$(psql -tAc "SELECT count(*) FROM collections WHERE collection_id='flowpay_docs_v1_qwen3_06b' AND status='ready'")
if [ "$ready" = 1 ]; then
    echo 'Knowledge collection already ready; preserving existing data.'
    exit 0
fi
# Finish decoding first: a failing producer in a pipe could otherwise leave psql
# committing a valid but incomplete prefix. Then import in one transaction.
restore_sql=$(mktemp)
trap 'rm -f "$restore_sql"' EXIT
pg_restore --data-only --disable-triggers --no-owner --no-acl \
    -t collections -t documents -t chunks --file="$restore_sql" /dataset/postgres/flowpay_knowledge.dump
psql --single-transaction --set ON_ERROR_STOP=1 --file="$restore_sql"
test "$(psql -tAc "SELECT count(*) FROM collections WHERE collection_id='flowpay_docs_v1_qwen3_06b' AND status='ready'")" = 1
echo 'Knowledge collection restored.'
