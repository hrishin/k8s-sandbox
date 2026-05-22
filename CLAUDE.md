# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Pulumi (Python) project that provisions a self-managed Kubernetes cluster on Scaleway bare metal servers using the `pulumiverse-scaleway` provider. It provisions 1 control plane node and N worker nodes, then exports shell scripts to bootstrap the cluster via kubeadm. State is stored locally at `~/.pulumi-local` (not in Pulumi Cloud).

## Setup

```bash
python -m venv venv
source venv/bin/activate   # or: direnv allow (uses .envrc)
pip install -r requirements.txt
```

## Common Commands

```bash
pulumi stack ls                  # list stacks
pulumi stack select dev          # switch to dev stack
pulumi preview                   # dry-run: show planned changes
pulumi up                        # deploy / apply changes
pulumi destroy                   # tear down all resources
pulumi stack output              # show exported values (IPs, scripts, etc.)
```

## Configuration

Stack config lives in `Pulumi.dev.yaml`. Configurable values:

| Key              | Default           | Description                                  |
|------------------|-------------------|----------------------------------------------|
| `zone`              | `fr-par-2`     | Scaleway availability zone                   |
| `instanceType`      | `PLAY2-MICRO`  | Scaleway instance commercial type            |
| `controlPlaneCount` | `1`            | Number of control plane nodes                |
| `workerCount`       | `1`            | Number of worker nodes                       |
| `lbType`            | `LB-S`         | Scaleway load balancer SKU (LB-S, LB-GP-M…) |
| `k8sVersion`        | `1.34`         | Kubernetes minor version to install          |

Set config values with:
```bash
pulumi config set workerCount 2
pulumi config set sshKeyName my-key
pulumi config set k8sVersion 1.34
```

## Architecture

`__main__.py` is the entire program — it:
1. Reads config values
2. Generates a persistent kubeadm join token and HA certificate key (stored in Pulumi state via `pulumi-random`)
3. Creates `controlPlaneCount` + `workerCount` Scaleway instances (`PLAY2-MICRO` by default, Ubuntu 22.04)
4. Creates a Scaleway Load Balancer fronting the control plane nodes on port 6443
5. Exports node IPs, LB IP, and per-node bootstrap commands

## Bootstrapping the Cluster

After `pulumi up`, SSH into each node and run its setup script:

The `.sh` files (`control-plane-init.sh`, `control-plane-join.sh`, `worker-join.sh`) live in the repo and accept positional arguments. Pulumi exports the per-node `bash -s -- <args>` command strings.

```bash
# Fetch all commands (contains secrets — handle with care)
pulumi stack output control_plane_setup_commands --show-secrets > cp-commands.json
pulumi stack output worker_setup_commands --show-secrets > worker-commands.json

# 1. First control plane (kubeadm init)
CP01_CMD=$(jq -r '.["control-plane-01"]' cp-commands.json)
ssh root@$(pulumi stack output control_plane_ips | jq -r '.["control-plane-01"]') \
    "$CP01_CMD" < control-plane-init.sh

# 2. Additional control planes (kubeadm join --control-plane) — can run in parallel
CP02_CMD=$(jq -r '.["control-plane-02"]' cp-commands.json)
CP03_CMD=$(jq -r '.["control-plane-03"]' cp-commands.json)
ssh root@<cp-02-ip> "$CP02_CMD" < control-plane-join.sh &
ssh root@<cp-03-ip> "$CP03_CMD" < control-plane-join.sh &
wait

# 3. Workers (kubeadm join)
W01_CMD=$(jq -r '.["worker-01"]' worker-commands.json)
ssh root@<worker-ip> "$W01_CMD" < worker-join.sh
```

To debug a script locally (dry-run without a real cluster):
```bash
bash control-plane-init.sh mytoken 1.2.3.4 5.6.7.8 1.34 <64-char-hex> control-plane-01
```

The scripts install:
- **containerd** (replaces Docker, removed in K8s 1.24+)
- **kubeadm / kubelet / kubectl** from `pkgs.k8s.io` (replaces deprecated `apt.kubernetes.io`)
- **Cilium CNI** (VXLAN tunnel mode; replaces Flannel)

## Scaleway Credentials

The provider reads `SCW_ACCESS_KEY` and `SCW_SECRET_KEY` (and optionally `SCW_DEFAULT_PROJECT_ID`) from environment variables. These must be set before running any `pulumi` command.
