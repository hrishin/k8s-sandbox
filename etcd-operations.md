# etcd SRE & Administration Runbook

Cluster: 3 control plane nodes (`control-plane-01..03`) + 1 worker, zone `fr-par-2`.

| Node | IP |
|------|----|
| control-plane-01 | `151.115.151.134` |
| control-plane-02 | `151.115.151.130` |
| control-plane-03 | `151.115.151.133` |
| worker-01 | `151.115.151.131` |

---

## Setup

SSH into any control plane node, then paste the block below in two steps. All subsequent commands in this runbook rely on these variables and the alias.

```bash
ssh root@151.115.151.134
```

### Step 1 — certs, local endpoint, alias

```bash
export ETCD_CACERT=/etc/kubernetes/pki/etcd/ca.crt
export ETCD_CERT=/etc/kubernetes/pki/etcd/server.crt
export ETCD_KEY=/etc/kubernetes/pki/etcd/server.key
export EP_LOCAL=https://127.0.0.1:2379

alias etcdctl='etcdctl \
  --endpoints=${EP_LOCAL} \
  --cacert=${ETCD_CACERT} \
  --cert=${ETCD_CERT} \
  --key=${ETCD_KEY}'
```

### Step 2 — discover members and export vars

Uses `etcdctl member list --write-out=json` to extract names and IPs dynamically, sorted by member name so CP01/CP02/CP03 are always stable.

```bash
eval $(etcdctl member list --write-out=json \
  | jq -r '
      .members
      | sort_by(.name)
      | to_entries[]
      | "export CP0\(.key + 1)_NAME=\(.value.name)
         export CP0\(.key + 1)_IP=\(.value.clientURLs[0] | gsub("https://|:2379"; ""))"
    ')

# Build the all-members endpoint string from discovered IPs
export EP_ALL="https://${CP01_IP}:2379,https://${CP02_IP}:2379,https://${CP03_IP}:2379"

# Verify
echo "CP01  ${CP01_NAME}  ${CP01_IP}"
echo "CP02  ${CP02_NAME}  ${CP02_IP}"
echo "CP03  ${CP03_NAME}  ${CP03_IP}"
echo "EP_ALL  ${EP_ALL}"
```

Expected output:
```
CP01  control-plane-01  151.115.151.134
CP02  control-plane-02  151.115.151.130
CP03  control-plane-03  151.115.151.133
EP_ALL  https://151.115.151.134:2379,https://151.115.151.130:2379,https://151.115.151.133:2379
```

> Workers are not etcd members — set `WORKER01_IP` manually if needed: `export WORKER01_IP=151.115.151.131`

---

## 1. Health & Status

```bash
# Single-node health check (uses alias → local endpoint)
etcdctl endpoint health

# All 3 members
etcdctl endpoint health --endpoints=${EP_ALL} \
  --cacert=${ETCD_CACERT} --cert=${ETCD_CERT} --key=${ETCD_KEY}

# Cluster status: leader, raft index, db size
etcdctl endpoint status --write-out=table --endpoints=${EP_ALL} \
  --cacert=${ETCD_CACERT} --cert=${ETCD_CERT} --key=${ETCD_KEY}

# DB size, usage, and leader status per member
etcdctl endpoint status --write-out=json --endpoints=${EP_ALL} \
  --cacert=${ETCD_CACERT} --cert=${ETCD_CERT} --key=${ETCD_KEY} \
| jq -r '
    .[]
    | {
        endpoint:          .Endpoint,
        role:              (if .Status.header.member_id == .Status.leader then "LEADER" else "follower" end),
        db_size_mb:        (.Status.dbSize      / 1048576 | . * 100 | round / 100),
        db_size_in_use_mb: (.Status.dbSizeInUse / 1048576 | . * 100 | round / 100),
        fragmented_mb:     ((.Status.dbSize - .Status.dbSizeInUse) / 1048576 | . * 100 | round / 100)
      }
    | "\(.role)\t\(.endpoint)\tdb_size=\(.db_size_mb) MB\tin_use=\(.db_size_in_use_mb) MB\tfragmented=\(.fragmented_mb) MB"
  ' | column -t

# List members and identify leader
etcdctl member list --write-out=table
```

---

## 2. Backup (Snapshot)

> Always run on the leader node. Check leadership via `endpoint status`.

```bash
# Take snapshot
etcdctl snapshot save /var/backups/etcd-snapshot-$(date +%Y%m%d-%H%M%S).db

# Verify snapshot integrity
etcdctl snapshot status /var/backups/etcd-snapshot-<timestamp>.db --write-out=table
```

---

## 3. Restore from Snapshot

> Run these steps on each control plane node individually, substituting `THIS_NAME` and `THIS_IP` for that node.

```bash
# Set on the node you are restoring (change per node)
THIS_NAME=${CP01_NAME}   # or CP02_NAME / CP03_NAME
THIS_IP=${CP01_IP}       # or CP02_IP / CP03_IP

INITIAL_CLUSTER="${CP01_NAME}=https://${CP01_IP}:2380,${CP02_NAME}=https://${CP02_IP}:2380,${CP03_NAME}=https://${CP03_IP}:2380"

# 1. Stop the API server
mv /etc/kubernetes/manifests/kube-apiserver.yaml /tmp/

# 2. Restore
etcdctl snapshot restore /var/backups/etcd-snapshot-<timestamp>.db \
  --name=${THIS_NAME} \
  --initial-cluster="${INITIAL_CLUSTER}" \
  --initial-cluster-token=etcd-cluster-restored \
  --initial-advertise-peer-urls=https://${THIS_IP}:2380 \
  --data-dir=/var/lib/etcd-restored

# 3. Point etcd static pod to new data dir, then restore API server
mv /tmp/kube-apiserver.yaml /etc/kubernetes/manifests/
```

---

## 4. Compaction + Defrag (Full Disk Reclaim Procedure)

These two operations are always done together in this fixed order:

| Step | Operation | Scope | Run on |
|------|-----------|-------|--------|
| 1 | Compaction | Cluster-wide (one call) | Any one node |
| 2 | Defrag | Per-member (3 calls) | Each node individually, followers first |
| 3 | Alarm disarm | Cluster-wide | Any one node (only if NOSPACE alarm was active) |

> Compaction marks old revisions as garbage across the whole cluster in a single call — you do **not** repeat it per node.
> Defrag is what physically reclaims the disk pages from each member's local bbolt file — it must be run on each node separately.
> Defrag causes ~1–2s unavailability on the target member. With 3 nodes quorum (2 of 3) is maintained throughout as long as you do one at a time.

### Step 1 — Compact (run once, on any node)

```bash
ssh root@${CP01_IP}

# Get the current cluster revision
REV=$(etcdctl endpoint status --write-out=json \
  | jq -r '.[0].Status.header.revision')

echo "Compacting at revision: $REV"

# Compact — applies cluster-wide, no need to repeat on other nodes
etcdctl compact $REV
```

### Step 2 — Defrag each member (followers first, leader last)

```bash
# Identify the leader before starting
etcdctl endpoint status --write-out=table --endpoints=${EP_ALL} \
  --cacert=${ETCD_CACERT} --cert=${ETCD_CERT} --key=${ETCD_KEY}

# Defrag follower: control-plane-02
etcdctl defrag \
  --endpoints=https://${CP02_IP}:2379 \
  --cacert=${ETCD_CACERT} --cert=${ETCD_CERT} --key=${ETCD_KEY}
echo "${CP02_NAME} done"

# Defrag follower: control-plane-03
etcdctl defrag \
  --endpoints=https://${CP03_IP}:2379 \
  --cacert=${ETCD_CACERT} --cert=${ETCD_CERT} --key=${ETCD_KEY}
echo "${CP03_NAME} done"

# Defrag the leader last
etcdctl defrag \
  --endpoints=https://${CP01_IP}:2379 \
  --cacert=${ETCD_CACERT} --cert=${ETCD_CERT} --key=${ETCD_KEY}
echo "${CP01_NAME} done"
```

### Step 3 — Verify and disarm alarms

```bash
# Confirm DB size shrank on all 3 members
etcdctl endpoint status --write-out=table --endpoints=${EP_ALL} \
  --cacert=${ETCD_CACERT} --cert=${ETCD_CERT} --key=${ETCD_KEY}

# Check for active alarms
etcdctl alarm list

# Disarm NOSPACE alarm if present (cluster-wide, one call)
etcdctl alarm disarm
```

---

## 5. Alarm Management

```bash
# List active alarms (NOSPACE is the most common)
etcdctl alarm list

# Disarm all alarms (only after fixing root cause, e.g. post-defrag)
etcdctl alarm disarm
```

---

## 7. Find Biggest Keys by Size

```bash
# Top 30 keys by value size (bytes)
etcdctl get / --prefix --write-out=json \
| jq -r '
    .kvs[]
    | {
        key: (.key | @base64d),
        size: (.value | @base64d | length)
      }
    | [.size, .key]
    | @tsv
  ' \
| sort -rn \
| head -30 \
| awk '{printf "%10d bytes  %s\n", $1, $2}'

# Top 20 keys in KB (hides sub-1KB entries)
etcdctl get / --prefix --write-out=json \
| jq -r '
    .kvs[]
    | {
        key: (.key | @base64d),
        size_kb: ((.value | @base64d | length) / 1024 | floor)
      }
    | select(.size_kb > 0)
    | "\(.size_kb) KB\t\(.key)"
  ' \
| sort -rn \
| head -20
```

Common culprits: `secrets` with large base64 payloads, `configmaps` with embedded certs, and `deployments`/`replicasets` with `kubectl.kubernetes.io/last-applied-configuration` annotation bloat. The per-key hard limit is **1.5 MB**.

### Space usage aggregated by resource type

Groups all keys by their `/registry/<resource>` prefix, totals the size, and sorts descending — shows you which resource types are dominating etcd storage.

```bash
etcdctl get / --prefix --write-out=json \
| jq -r '
    [
      .kvs[]
      | {
          prefix: (.key | @base64d | split("/") | .[0:3] | join("/")),
          size:   (.value | @base64d | length)
        }
    ]
    | group_by(.prefix)
    | map({
        prefix:   .[0].prefix,
        total_mb: ((map(.size) | add) / 1048576 | . * 100 | round / 100),
        count:    length
      })
    | sort_by(-.total_mb)
    | .[]
    | "\(.total_mb) MB\t\(.count) keys\t\(.prefix)"
  ' \
| column -t
```

Expected output:
```
0.45 MB  142 keys  /registry/pods
0.32 MB   89 keys  /registry/secrets
0.18 MB   67 keys  /registry/deployments
0.11 MB   34 keys  /registry/replicasets
0.04 MB   12 keys  /registry/configmaps
...
```

> The prefix split takes the first 3 path segments (`/` + `registry` + `<resource-type>`), so cluster-scoped and namespaced resources both collapse to the same resource-type bucket. Drill into a specific type with the individual key query above.

---

## 8. Key Inspection (Read-Only Debugging)

```bash
# Total key count
etcdctl get / --prefix --keys-only | wc -l

# All keys under a namespace
etcdctl get /registry/pods/kube-system --prefix --keys-only

# Inspect a specific resource as JSON
etcdctl get /registry/pods/<namespace>/<pod-name> -w json | jq .

# Count secrets
etcdctl get /registry/secrets --prefix --keys-only | wc -l
```

---

## 9. Member Management (Disaster Recovery)

> Never remove more than 1 member at a time — quorum requires 2 of 3.

```bash
# List members with IDs
etcdctl member list --write-out=table

# Remove a failed member
etcdctl member remove <member-id>

# Register a replacement member before starting it
etcdctl member add ${CP02_NAME} --peer-urls=https://${CP02_IP}:2380

# Update a member's peer URL
etcdctl member update <member-id> --peer-urls=https://${CP02_IP}:2380
```

---

## 10. Watch (Live Key Changes)

```bash
# Watch all pod changes
etcdctl watch /registry/pods --prefix

# Watch with JSON output (useful for piping to jq)
etcdctl watch /registry --prefix -w json
```

---

## 11. Monitoring via kubectl (from local machine)

```bash
export KUBECONFIG=/Users/hrishis/code/scaleaway/kubeconfig-scaleaway.yaml

export CP01_NAME=control-plane-01
export CP02_NAME=control-plane-02
export CP03_NAME=control-plane-03

# Logs
kubectl logs -n kube-system etcd-${CP01_NAME}
kubectl logs -n kube-system etcd-${CP02_NAME}
kubectl logs -n kube-system etcd-${CP03_NAME}

# Pod details
kubectl describe pod -n kube-system etcd-${CP01_NAME}

# Resource usage
kubectl top pod -n kube-system -l component=etcd
```

---

## 12. Grafana & Datadog Monitoring Queries

Each concern maps a monitoring signal → the runbook section that fixes it. Metric names are from the [official etcd v3.4 reference](https://etcd.io/docs/v3.4/metrics/etcd-metrics-v3.4.0/).

> **Grafana**: PromQL — assumes etcd metrics are scraped via `kube-prometheus-stack` or a Prometheus scrape job with `job="etcd"`.
> **Datadog**: OpenMetrics integration; metric names follow Prometheus naming. Replace `{host:control-plane-*}` with your host tag scope.

---

### 12.1 Cluster health & leader existence

**Concern**: has the cluster lost a leader entirely? Is every member reachable?
**Runbook**: [§1 Health & Status](#1-health--status), [§9 Member Management](#9-member-management-disaster-recovery)

```promql
# Grafana — scrape target up/down per member (1 = up)
up{job="etcd"}

# Grafana — does ANY leader exist on this member's view (0 = no leader = total outage)
etcd_server_has_leader

# Grafana — is this specific instance the current leader
etcd_server_is_leader

# Grafana — leader changes in last hour (> 1 = instability)
increase(etcd_server_leader_changes_seen_total[1h])

# Grafana — heartbeat send failures (rising = leader can't reach peers)
rate(etcd_server_heartbeat_send_failures_total[5m])

# Grafana — alert: no leader on any member
min(etcd_server_has_leader) == 0

# Grafana — alert: leader changed more than once in 15 min
increase(etcd_server_leader_changes_seen_total[15m]) > 1
```

```
# Datadog — leader existence (0 = cluster has no leader)
min:etcd.server.has_leader{host:control-plane-*}

# Datadog — which instance is leader
avg:etcd.server.is_leader{*} by {host}

# Datadog — leader election frequency
sum:etcd.server.leader_changes_seen_total{*}.as_count()

# Datadog — heartbeat failures rate
sum:etcd.server.heartbeat_send_failures_total{*}.as_rate()

# Datadog monitor — no leader
min:etcd.server.has_leader{host:control-plane-*} < 1
```

---

### 12.2 DB size and fragmentation

**Concern**: DB approaching 2 GB quota; NOSPACE alarm imminent.
**Runbook**: [§4 Compaction + Defrag](#4-compaction--defrag-full-disk-reclaim-procedure)

```promql
# Grafana — total allocated DB file size per member
etcd_mvcc_db_total_size_in_bytes

# Grafana — live data actually in use (post-compaction logical size)
etcd_mvcc_db_total_size_in_use_in_bytes

# Grafana — fragmented bytes reclaimable by defrag
etcd_mvcc_db_total_size_in_bytes - etcd_mvcc_db_total_size_in_use_in_bytes

# Grafana — fragmentation ratio (> 0.5 = defrag recommended)
1 - (etcd_mvcc_db_total_size_in_use_in_bytes / etcd_mvcc_db_total_size_in_bytes)

# Grafana — storage quota ceiling (set via --quota-backend-bytes, default 2 GB)
etcd_server_quota_backend_bytes

# Grafana — DB as % of quota
etcd_mvcc_db_total_size_in_bytes / etcd_server_quota_backend_bytes

# Grafana — alert: DB size > 75% of quota
(etcd_mvcc_db_total_size_in_bytes / etcd_server_quota_backend_bytes) > 0.75
```

```
# Datadog
avg:etcd.mvcc.db.total_size_in_bytes{*} by {host}
avg:etcd.mvcc.db.total_size_in_use_in_bytes{*} by {host}

# Datadog — fragmented bytes
avg:etcd.mvcc.db.total_size_in_bytes{*} - avg:etcd.mvcc.db.total_size_in_use_in_bytes{*}

# Datadog — fragmentation ratio
1 - (avg:etcd.mvcc.db.total_size_in_use_in_bytes{*} / avg:etcd.mvcc.db.total_size_in_bytes{*})

# Datadog monitor — DB > 75% of 2 GB quota
avg:etcd.mvcc.db.total_size_in_bytes{*} by {host} > 1500000000
```

---

### 12.3 Disk I/O latency

**Concern**: slow WAL or backend fsync causes request timeouts and triggers leader elections.
**Runbook**: [§1 Health & Status](#1-health--status), investigate disk with `iostat`/`iotop`

```promql
# Grafana — WAL fsync p99 latency
# etcd_disk_wal_fsync_duration_seconds (histogram)
# healthy: < 10 ms  |  concerning: > 10 ms  |  critical: > 100 ms
histogram_quantile(0.99,
  rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])
)

# Grafana — backend commit p99 latency
# etcd_disk_backend_commit_duration_seconds (histogram)
# healthy: < 25 ms  |  concerning: > 25 ms
histogram_quantile(0.99,
  rate(etcd_disk_backend_commit_duration_seconds_bucket[5m])
)

# Grafana — defrag duration (while defrag is running)
# etcd_disk_backend_defrag_duration_seconds (histogram)
histogram_quantile(0.99,
  rate(etcd_disk_backend_defrag_duration_seconds_bucket[5m])
)

# Grafana — alert: WAL p99 > 10 ms
histogram_quantile(0.99,
  rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])
) > 0.01

# Grafana — alert: backend commit p99 > 25 ms
histogram_quantile(0.99,
  rate(etcd_disk_backend_commit_duration_seconds_bucket[5m])
) > 0.025
```

```
# Datadog
p99:etcd.disk.wal_fsync_duration_seconds{*} by {host}
p99:etcd.disk.backend_commit_duration_seconds{*} by {host}

# Datadog monitor — WAL p99 > 10 ms
p99:etcd.disk.wal_fsync_duration_seconds{*} by {host} > 0.01
```

---

### 12.4 Raft proposals (write health)

**Concern**: failed or stacked proposals mean writes are dropped or stalled.
**Runbook**: [§1 Health & Status](#1-health--status), [§5 Alarm Management](#5-alarm-management)

`proposals_committed_total` and `proposals_applied_total` are **gauges** in v3.4 (monotonically increasing, not reset on restart).

```promql
# Grafana — failed proposals (should be 0; spikes during elections)
rate(etcd_server_proposals_failed_total[5m])

# Grafana — pending proposals (gauge; rising = backlog building)
etcd_server_proposals_pending

# Grafana — committed vs applied lag
# both are gauges; the difference should stay near 0
etcd_server_proposals_committed_total - etcd_server_proposals_applied_total

# Grafana — slow applies (disk overload indicator)
rate(etcd_server_slow_apply_total[5m])

# Grafana — alert: any failed proposals
rate(etcd_server_proposals_failed_total[5m]) > 0

# Grafana — alert: committed/applied gap > 5000 (cluster falling behind)
(etcd_server_proposals_committed_total - etcd_server_proposals_applied_total) > 5000
```

```
# Datadog
sum:etcd.server.proposals_failed_total{*}.as_rate()
avg:etcd.server.proposals_pending{*} by {host}
avg:etcd.server.proposals_committed_total{*} - avg:etcd.server.proposals_applied_total{*}
sum:etcd.server.slow_apply_total{*}.as_rate()

# Datadog monitor
sum:etcd.server.proposals_failed_total{*}.as_rate() > 0
```

---

### 12.5 Peer network health

**Concern**: high RTT or send/receive failures between CP nodes cause election timeouts and replication stalls.
**Runbook**: [§1 Health & Status](#1-health--status)

```promql
# Grafana — p99 peer RTT per member pair (label: To="<peer-id>")
# etcd_network_peer_round_trip_time_seconds (histogram)
# healthy: < 50 ms  |  concerning: > 100 ms
histogram_quantile(0.99,
  rate(etcd_network_peer_round_trip_time_seconds_bucket[5m])
) by (To)

# Grafana — peer send failures per peer
rate(etcd_network_peer_sent_failures_total[5m]) by (To)

# Grafana — peer receive failures per peer
rate(etcd_network_peer_received_failures_total[5m]) by (From)

# Grafana — active peers (should be 2 for a 3-node cluster per member)
etcd_network_active_peers

# Grafana — alert: p99 RTT > 100 ms to any peer
max by (To) (
  histogram_quantile(0.99,
    rate(etcd_network_peer_round_trip_time_seconds_bucket[5m])
  )
) > 0.1

# Grafana — alert: any peer send failures
rate(etcd_network_peer_sent_failures_total[5m]) > 0
```

```
# Datadog
p99:etcd.network.peer_round_trip_time_seconds{*} by {host}
sum:etcd.network.peer_sent_failures_total{*}.as_rate() by {host}
sum:etcd.network.peer_received_failures_total{*}.as_rate() by {host}
avg:etcd.network.active_peers{*} by {host}

# Datadog monitor — RTT > 100 ms
p99:etcd.network.peer_round_trip_time_seconds{*} by {host} > 0.1
```

---

### 12.6 Key count, slow reads, and watch health

**Concern**: too many keys or watchers slowing reads; unbounded controllers creating key accumulation.
**Runbook**: [§7 Biggest Keys](#7-find-biggest-keys-by-size), [§8 Key Inspection](#8-key-inspection-read-only-debugging)

```promql
# Grafana — total keys in etcd
# NOTE: correct metric is etcd_debugging_mvcc_keys_total (not etcd_mvcc_keys_total)
etcd_debugging_mvcc_keys_total

# Grafana — slow linearizable read indexes
rate(etcd_server_slow_read_indexes_total[5m])

# Grafana — failed read index operations
rate(etcd_server_read_indexes_failed_total[5m])

# Grafana — slow applies (disk overload causing reads to back up)
rate(etcd_server_slow_apply_total[5m])

# Grafana — total watch subscriptions
etcd_debugging_mvcc_watcher_total

# Grafana — unsynced (slow) watchers — rising = consumers can't keep up
etcd_debugging_mvcc_slow_watcher_total

# Grafana — pending watch events not yet delivered
etcd_debugging_mvcc_pending_events_total

# Grafana — alert: slow watchers > 0 sustained
etcd_debugging_mvcc_slow_watcher_total > 0
```

```
# Datadog
avg:etcd.debugging.mvcc.keys_total{*} by {host}
sum:etcd.server.slow_read_indexes_total{*}.as_rate()
sum:etcd.server.read_indexes_failed_total{*}.as_rate()
avg:etcd.debugging.mvcc.watcher_total{*} by {host}
avg:etcd.debugging.mvcc.slow_watcher_total{*} by {host}
avg:etcd.debugging.mvcc.pending_events_total{*} by {host}
```

---

### 12.7 Snapshot duration

**Concern**: snapshot save taking too long, indicating disk slowness that will affect backup reliability.
**Runbook**: [§2 Backup](#2-backup-snapshot)

```promql
# Grafana — v3 snapshot save p99 duration
# etcd_snap_db_save_total_duration_seconds (histogram)
histogram_quantile(0.99,
  rate(etcd_snap_db_save_total_duration_seconds_bucket[5m])
)

# Grafana — snapshot fsync duration
histogram_quantile(0.99,
  rate(etcd_snap_fsync_duration_seconds_bucket[5m])
)
```

```
# Datadog
p99:etcd.snap.db_save_total_duration_seconds{*}
p99:etcd.snap.fsync_duration_seconds{*}
```

---

### 12.8 Process / OS resource limits

**Concern**: file descriptor exhaustion causes etcd to panic and crash.
**Runbook**: [§1 Health & Status](#1-health--status), [§9 Member Management](#9-member-management-disaster-recovery)

```promql
# Grafana — open file descriptors
process_open_fds{job="etcd"}

# Grafana — max file descriptor limit
process_max_fds{job="etcd"}

# Grafana — fd usage ratio (> 0.8 is dangerous)
process_open_fds{job="etcd"} / process_max_fds{job="etcd"}

# Grafana — alert: fd usage > 80%
(process_open_fds{job="etcd"} / process_max_fds{job="etcd"}) > 0.8
```

```
# Datadog
avg:process.open_file_descriptors{service:etcd} by {host}
avg:process.max_file_descriptors{service:etcd} by {host}

# Datadog monitor — fd usage > 80%
avg:process.open_file_descriptors{service:etcd} / avg:process.max_file_descriptors{service:etcd} > 0.8
```

---

### Signal → Action cheat sheet

| Monitoring signal | Metric | Threshold | Runbook action |
|---|---|---|---|
| No leader | `etcd_server_has_leader` | = 0 | §1 health check; §9 member recovery |
| Leader thrash | `etcd_server_leader_changes_seen_total` | > 1 / 15 min | Check §12.3 disk, §12.5 peer RTT |
| Heartbeat failures | `etcd_server_heartbeat_send_failures_total` | > 0 | Check peer connectivity (§12.5) |
| DB > quota threshold | `etcd_mvcc_db_total_size_in_bytes` | > 75% of quota | §4 Compact + defrag |
| High fragmentation | fragmentation ratio | > 50% | §4 Defrag |
| WAL fsync slow | `etcd_disk_wal_fsync_duration_seconds` p99 | > 10 ms | Move to dedicated SSD; `iostat` |
| Backend commit slow | `etcd_disk_backend_commit_duration_seconds` p99 | > 25 ms | Check disk I/O |
| Failed proposals | `etcd_server_proposals_failed_total` | > 0 | §5 alarms; §1 member health |
| Proposals backlog | `etcd_server_proposals_pending` | > 0 sustained | Leader overloaded; check disk + rate |
| Committed/applied gap | `committed - applied` | > 5000 | Disk or CPU overload on leader |
| Peer RTT high | `etcd_network_peer_round_trip_time_seconds` p99 | > 100 ms | Network between CP nodes |
| Peer send failures | `etcd_network_peer_sent_failures_total` | > 0 | Network; §9 if member unreachable |
| Slow watchers | `etcd_debugging_mvcc_slow_watcher_total` | > 0 sustained | §7 key inspection; audit controllers |
| Key accumulation | `etcd_debugging_mvcc_keys_total` | > 500 k | §7 biggest key types; prune resources |
| FD exhaustion risk | `process_open_fds / process_max_fds` | > 80% | Raise `ulimit -n`; restart etcd |
| Member down | `up{job="etcd"}` | < 3 | §9 restore before losing another |

---

## Quick Reference

| Concern | Threshold | Action |
|---------|-----------|--------|
| DB size | > 2 GB | Compact + defrag; raise `--quota-backend-bytes` if needed |
| NOSPACE alarm | Any | Defrag → compact → `alarm disarm` |
| Single large key | > 1.5 MB | Find with biggest-keys query; split or prune |
| Leader election thrash | Frequent | Check disk latency (`iotop`), network jitter between CPs |
| Member down | 1 of 3 | Cluster still serves (quorum = 2); restore before losing another |
