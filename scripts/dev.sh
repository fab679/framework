#!/usr/bin/env bash
set -euo pipefail
# Local dev entrypoint.
pnpm install
pnpm --filter @fabisch/agents test --run
echo "try: pnpm --filter @fabisch/agents dev"
