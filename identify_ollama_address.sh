#!/usr/bin/env bash
set -euo pipefail

ENV_FILE=".env"

# 1. Extract the default gateway IP
DEFAULT_IP=$(ip route show default | awk '/default via/ {print $3; exit}')

if [[ -z "$DEFAULT_IP" ]]; then
    echo "Error: Could not determine default gateway IP."
    exit 1
fi

# 2. Make sure the .env file exists
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: $ENV_FILE not found."
    exit 1
fi

# 3. Update only OLLAMA_HOST_PATH
if grep -q '^OLLAMA_HOST_PATH=' "$ENV_FILE"; then
    sed -i "s|^OLLAMA_HOST_PATH=.*|OLLAMA_HOST_PATH=${DEFAULT_IP}|" "$ENV_FILE"
else
    echo "OLLAMA_HOST_PATH=${DEFAULT_IP}" >> "$ENV_FILE"
fi

echo "OLLAMA_HOST_PATH updated to: $DEFAULT_IP"