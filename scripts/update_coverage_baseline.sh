#!/usr/bin/env bash
# Update coverage and test-count baseline files.
# Usage: ./scripts/update_coverage_baseline.sh
#
# Runs the full test suite with coverage, extracts the total coverage
# percentage and test count, and writes them to baseline files at the
# repo root.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "→ Running full test suite with coverage..."
output=$(KOAN_ROOT=/tmp/test-koan make test 2>&1) || true

# Extract coverage percentage from TOTAL line (e.g., "TOTAL  23741  22779  4.1%")
coverage=$(echo "$output" | grep -E '^TOTAL\s' | awk '{print $NF}' | tr -d '%')
if [ -z "$coverage" ]; then
    echo "ERROR: Could not extract coverage percentage from test output."
    echo "$output" | tail -20
    exit 1
fi

# Extract test count from pytest summary line (e.g., "11075 passed in 132.5s")
test_count=$(echo "$output" | grep -oE '[0-9]+ passed' | awk '{print $1}')
if [ -z "$test_count" ]; then
    echo "ERROR: Could not extract test count from test output."
    echo "$output" | tail -20
    exit 1
fi

echo "$coverage" > coverage-baseline.txt
echo "$test_count" > test-count-baseline.txt

echo "✓ Coverage baseline: ${coverage}%"
echo "✓ Test count baseline: ${test_count}"
echo "  Files updated: coverage-baseline.txt, test-count-baseline.txt"
