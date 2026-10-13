#!/usr/bin/env bash
set -euo pipefail
# Clean all build artefacts.
rm -rf node_modules packages/typescript/node_modules packages/typescript/dist coverage .turbo
echo "cleaned."
