#!/usr/bin/env bash
# Update coverage and test count baselines after running the full test suite.
# Usage: ./scripts/update-coverage-baseline.sh
#
# This script runs `make test` (which includes --cov), then extracts the
# total coverage percentage and test count into their baseline files.

set -euo pipefail

cd "$(dirname "$0")/.."

echo "→ Running full test suite with coverage..."
make test 2>&1 | tee /tmp/koan-coverage-output.txt

# Extract coverage total from the TOTAL line
COVERAGE=$(grep '^TOTAL' /tmp/koan-coverage-output.txt | awk '{print $NF}' | tr -d '%')
if [ -z "$COVERAGE" ]; then
    echo "Error: Could not extract coverage percentage from test output"
    exit 1
fi

# Extract test count from pytest summary line (e.g., "10993 passed in 110.93s")
TEST_COUNT=$(grep -oE '[0-9]+ passed' /tmp/koan-coverage-output.txt | tail -1 | awk '{print $1}')
if [ -z "$TEST_COUNT" ]; then
    echo "Error: Could not extract test count from test output"
    exit 1
fi

echo "$COVERAGE" > coverage-baseline.txt
echo "$TEST_COUNT" > test-count-baseline.txt

echo ""
echo "✓ Baselines updated:"
echo "  Coverage:   ${COVERAGE}%"
echo "  Test count: ${TEST_COUNT}"

rm -f /tmp/koan-coverage-output.txt
