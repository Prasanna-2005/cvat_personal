#!/bin/bash
set -euo pipefail

set -a
source .env
set +a

echo "[+] Turning off CVAT"
docker compose down

echo "[+] Building frontend container"
docker compose build cvat_ui

echo "[+] Deploying all containers"
docker compose up -d

echo "[+] Deploying all functions"
bash ./serverless/deploy_cpu.sh ./serverless/custom/
