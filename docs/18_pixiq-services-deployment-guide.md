# Pixiq Services Deployment Guide

> Target device: Jetson Orin NX
> Final expected state: project files are present on the Jetson and the `sieger-inspection` and `sieger-api` services are running.

---

## Overview

Deployment is now a local-machine-to-Jetson flow:

1. Clone or prepare the project on the local developer/build machine.
2. Transfer the project folder to the Jetson with `rsync`.
3. SSH into the Jetson, create the data root folder, and set `data_root` in `src/config.json`.
4. Run `scripts/deploy.sh` (no `sudo`).

The deploy script performs the Jetson-side setup: system tools, rclone validation, Python environment, site configuration, systemd service installation, and health checks.

By default, `deploy.sh` skips DVC setup, model file pull, and TensorRT export because these are not needed during a normal new-site installation. The model files and TensorRT engine files should already be included in the transferred project folder.

---

## Local Machine Setup (Developer / Build Machine)

Run these steps on the developer/build machine.

### Step 1: Clone the Repository

```bash
git clone <repo>
cd CTS-PIXIQ
```

Replace `<repo>` with the actual repository URL if needed.

Before transfer, confirm the folder contains the deployment script:

```bash
ls scripts/deploy.sh
```

Also confirm required runtime assets are present before sending to the Jetson:

```bash
ls weights/*.pt
ls weights/*.engine
```

---

### Step 2: Transfer Files to Jetson

Using rsync over SSH (Recommended)

```bash
# From your local machine
JETSON_USER="dhvaniai"
JETSON_IP="192.168.3.XXX" # Change to your Jetson's IP
PROJECT_NAME="CTS-PIXIQ"

# Create target folder on Jetson and sync
ssh "$JETSON_USER@$JETSON_IP" "mkdir -p /home/$JETSON_USER/$PROJECT_NAME"

rsync -avz --progress --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pth' \
  --exclude='.dvc/cache' \
  . "$JETSON_USER@$JETSON_IP:/home/$JETSON_USER/$PROJECT_NAME"
```

After the transfer, the code should exist on the Jetson at:

```bash
/home/dhvaniai/CTS-PIXIQ
```

If your Jetson username or project folder is different, use that path in the remaining commands.

---

## Jetson Deployment

Run the remaining steps on the Jetson.

### Step 3: SSH Into the Jetson

```bash
ssh dhvaniai@192.168.3.XXX
```

Then enter the transferred project folder:

```bash
cd ~/CTS-PIXIQ
```

Make sure the deploy script is executable:

```bash
chmod +x scripts/deploy.sh
```

### Step 4: Prepare the Data Root on the Jetson

Before running the deploy script, set up the data root directory and point the config at it.

1. Create the data folder in the Jetson user's home directory:

```bash
mkdir -p /home/pixiq/cone_transport_system_pixiq
```

2. Set `data_root` in `src/config.json` to that exact path:

```bash
cd ~/CTS-PIXIQ
nano src/config.json
```

Set:

```json
"data_root": "/home/pixiq/cone_transport_system_pixiq",
```

Save the file before continuing.

### Step 5: Run Deployment

```bash
./scripts/deploy.sh
```

The script is safe to re-run after a failure. It records a deployment report under `/tmp/pixiq_deploy_report_<timestamp>.json`.

### What `deploy.sh` Does

| Phase | What happens |
|---|---|
| 1. Pre-flight | Validates architecture, JetPack, GPU access, and network connectivity |
| 2. Tools Installation | Runs `system_setup.sh`, verifies system tools, CUDA/TensorRT, Pylon SDK, `uv`, and `systemctl` |
| 3. Rclone Server Setup | Installs/verifies `rclone`, checks `sieger_azure`, verifies Azure container access, and creates `/var/log/rclone_sieger.log` |
| 4. DVC Setup | Skipped by default for new installation sites |
| 5. Model Files | Skipped by default; model files should already be in the transferred project |
| 6. Python Environment | Creates/reuses Python 3.10 venv and runs `uv sync` |
| 7. Configuration | Backs up and validates `src/config.json` |
| 8. TensorRT Export | Skipped by default; `.engine` files should already be in the transferred project |
| 9. Service Installation | Installs and starts `sieger-inspection` and `sieger-api` |
| 10. Health Validation | Checks API health and systemd service status |

---

## Required One-Time Rclone Setup

`rclone` is required for Azure Blob upload from the teaching workflow. The deploy script checks this during Phase 3.

If the remote is missing, run:

```bash
rclone config
```

Use these values:

```text
Type n for New remote
Name: sieger_azure
Storage type: 21
account: trainingdatasetdhvani
key: XlFct0TwudmbcM47xv9QQWNKrLGQR7F38MW8vT2DXNEWxpurwnaxNuDZN4z+kxoVyverhNQBny2f+AStJSKRxw==
sas_url: press Enter to skip
use_emulator: press Enter (default false)
Edit advanced config: n
Confirm: y
Quit: q
```

Verify the remote:

```bash
rclone lsd sieger_azure:
rclone ls sieger_azure:raw-batches
```

The deploy script also creates the upload log file:

```bash
sudo touch /var/log/rclone_sieger.log
sudo chmod 666 /var/log/rclone_sieger.log
```

After completing rclone config, re-run:

```bash
./scripts/deploy.sh
```

---

## Optional Model Asset Refresh

For normal site deployment, do not run this.

Only use this mode when the Jetson must pull model files from DVC and export TensorRT engines locally:

```bash
RUN_MODEL_ASSET_SETUP=true ./scripts/deploy.sh
```

This enables:

- DVC Azure setup
- `.pt` model pull
- TensorRT `.engine` export

---

## Verify Final Deployment

After `deploy.sh` completes successfully, confirm the project files are still present:

```bash
cd ~/CTS-PIXIQ
ls scripts/deploy.sh src/config.json weights
```

Check both services:

```bash
sudo systemctl status sieger-inspection sieger-api
```

Check API health:

```bash
curl http://localhost:5002/health
curl http://localhost:5002/health/system
```

View logs if needed:

```bash
journalctl -u sieger-inspection -f
journalctl -u sieger-api -f
```

The deployment is complete when:

- The project folder exists on the Jetson.
- `src/config.json` has site-specific values.
- Required `weights/*.pt` and `weights/*.engine` files are present.
- `rclone` remote `sieger_azure` works.
- `sieger-inspection` is active.
- `sieger-api` is active.
- `http://localhost:5002/health` responds successfully.

---

## Deployment Status Report

To view the latest deployment status:

```bash
./scripts/deploy.sh --status
```

The latest report is stored in:

```bash
/tmp/pixiq_deploy_report_<timestamp>.json
```

---

## Common Fixes

If `rclone_setup` fails, configure `sieger_azure` with `rclone config`, then rerun `./scripts/deploy.sh`.

If `configuration` warns about placeholders, edit:

```bash
nano src/config.json
```

If services fail to start, inspect logs:

```bash
journalctl -u sieger-inspection -n 100
journalctl -u sieger-api -n 100
```

If the deploy script reports a reboot is required:

```bash
sudo reboot
```

Then SSH back in and rerun:

```bash
cd ~/CTS-PIXIQ
./scripts/deploy.sh
```
