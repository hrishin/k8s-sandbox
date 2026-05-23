# PromQL Exercises — Kubernetes SRE via Grafana Explore

Cluster: 3 control plane nodes + 1 worker, `kube-prometheus-stack` installed.

## Access

```
http://151.115.151.131:32000   admin / admin
Left sidebar → Explore → Datasource: Prometheus
```

## Grafana Explore tips

| Action | How |
|---|---|
| Run query | `Shift + Enter` |
| Code vs Builder mode | toggle top-right of query box |
| Best view for instant vectors | **Table** tab |
| Best view for rates over time | **Graph** tab |
| Overlay two queries | `+ Add query` |
| Side-by-side comparison | `Split` button |
| See exact labels on a series | click series name in legend |
| Inspect raw JSON response | `Query inspector` → `Data` |

> **Rule of thumb**: use `$__rate_interval` everywhere you need a rate window —
> Grafana auto-sizes it to `max(4 × scrape_interval, $__interval)` which is
> always correct regardless of zoom level. Never hardcode `[5m]`.

---

## Module 1 — PromQL Foundations

---

### 1.1 — How many series does each metric have?

**View**: Table | **Mode**: Code

```promql
sort_desc(count by (__name__) ({__name__=~".+"}))
```

**What to look for**: `container_*` metrics will have the highest counts because
they multiply across pod × container × namespace × node × interface. These are
your cardinality hotspots.

**Bonus — count total series in Prometheus:**
```promql
count({__name__=~".+"})
```

---

### 1.2 — Browse etcd metrics

**View**: Table | **Mode**: Code

```promql
{__name__=~"etcd_.+"}
```

Switch to **Builder** mode → click the metric dropdown → type `etcd_` to browse
all available etcd metrics with their descriptions.

**Filter to only gauge-type etcd metrics:**
```promql
{__name__=~"etcd_.+", __name__!~"etcd_.+_total|etcd_.+_bucket|etcd_.+_sum|etcd_.+_count"}
```

---

### 1.3 — Counter raw value vs rate()

**View**: Graph | **Mode**: Code

**Step 1 — raw counter (useless for graphing, always rising):**
```promql
container_cpu_usage_seconds_total{
  namespace="monitoring",
  pod=~"prometheus-.*"
}
```

**Step 2 — rate turns it into CPU cores used per second:**
```promql
rate(container_cpu_usage_seconds_total{
  namespace="monitoring",
  pod=~"prometheus-.*"
}[$__rate_interval])
```

**What changed**: the raw counter is a monotonically increasing number of
seconds. `rate()` divides the delta by the time window → result is cores/sec
(e.g. `0.05` = 5% of one CPU core).

---

### 1.4 — increase() vs rate()

**View**: Table | **Mode**: Code

**Bytes received by Prometheus pod in the last 5 minutes:**
```promql
increase(container_network_receive_bytes_total{
  pod=~"prometheus-kube-prometheus-stack-prometheus-0"
}[5m])
```

**Same result via rate() × window seconds:**
```promql
rate(container_network_receive_bytes_total{
  pod=~"prometheus-kube-prometheus-stack-prometheus-0"
}[5m]) * 300
```

**Key difference**:
| Function | Returns | Use for |
|---|---|---|
| `rate()` | per-second average | graphs, alerts |
| `increase()` | total delta over window | "how much in last N min" |
| `irate()` | per-second using last 2 samples only | spiky real-time data |

---

### 1.5 — Aggregation operators

**View**: Table | **Mode**: Code

**Total CPU cores used across all pods in monitoring namespace:**
```promql
sum(rate(container_cpu_usage_seconds_total{
  namespace="monitoring",
  container!=""
}[$__rate_interval]))
```

**Break it down per pod:**
```promql
sum by (pod) (rate(container_cpu_usage_seconds_total{
  namespace="monitoring",
  container!=""
}[$__rate_interval]))
```

**Top 3 CPU-consuming pods:**
```promql
topk(3,
  sum by (pod) (rate(container_cpu_usage_seconds_total{
    namespace="monitoring",
    container!=""
  }[$__rate_interval]))
)
```

---

### 1.6 — Regex label selectors

**View**: Graph | **Mode**: Code

**Network receive rate for all kube-prometheus pods:**
```promql
rate(container_network_receive_bytes_total{
  namespace="monitoring",
  pod=~"kube-prometheus-stack-.*"
}[$__rate_interval])
```

**Exclude the operator pod:**
```promql
rate(container_network_receive_bytes_total{
  namespace="monitoring",
  pod=~"kube-prometheus-stack-.*",
  pod!~".*operator.*"
}[$__rate_interval])
```

| Operator | Meaning |
|---|---|
| `=` | exact match |
| `!=` | exact not match |
| `=~` | regex match |
| `!~` | regex not match |

---

## Module 2 — Node Health

---

### 2.1 — CPU utilisation % per node

**View**: Graph | **Mode**: Code

```promql
(1 - avg by (instance) (
  rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval])
)) * 100
```

**How it works**:
- `node_cpu_seconds_total` has one series per CPU core per mode (`idle`, `user`, `system`, `iowait`, ...)
- `mode="idle"` is the fraction of time each core is doing nothing
- `avg by (instance)` averages across all cores on that node
- `1 - idle = busy`, multiply by 100 for %

**Bonus — break down by CPU mode (user/system/iowait):**
```promql
sum by (instance, mode) (
  rate(node_cpu_seconds_total{mode!="idle"}[$__rate_interval])
) * 100
```

---

### 2.2 — Memory usage % per node

**View**: Graph | **Mode**: Code

```promql
(1 - (
  node_memory_MemAvailable_bytes /
  node_memory_MemTotal_bytes
)) * 100
```

**Why `MemAvailable` not `MemFree`**: Linux uses free memory for buffer/cache
which it reclaims instantly under pressure. `MemAvailable` accounts for this —
it is what the kernel reports as truly available to new processes.

**Bonus — absolute usage in GB:**
```promql
(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / 1073741824
```

---

### 2.3 — Disk usage % per node

**View**: Table | **Mode**: Code

```promql
(1 - (
  node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} /
  node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}
)) * 100
```

Filter `fstype!~"tmpfs|overlay|squashfs"` removes virtual/container filesystems
that would pollute the results.

**Find the fullest filesystem:**
```promql
topk(1,
  (1 - (
    node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"} /
    node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"}
  )) * 100
)
```

---

### 2.4 — Node Ready condition

**View**: Table | **Mode**: Code

**All nodes and their Ready status (1 = Ready, 0 = not Ready):**
```promql
kube_node_status_condition{condition="Ready", status="true"}
```

**Alert expression — fires when any node is not Ready:**
```promql
kube_node_status_condition{condition="Ready", status="true"} == 0
```

**Bonus — count of Ready vs not Ready nodes:**
```promql
count by (status) (kube_node_status_condition{condition="Ready"})
```

---

### 2.5 — Disk I/O throughput per node

**View**: Graph | **Mode**: Code

**Read throughput in MB/s:**
```promql
sum by (instance) (
  rate(node_disk_read_bytes_total[$__rate_interval])
) / 1048576
```

**Write throughput in MB/s:**
```promql
sum by (instance) (
  rate(node_disk_written_bytes_total[$__rate_interval])
) / 1048576
```

**Add both queries to the same panel** using `+ Add query` to overlay read and
write on the same graph. Set legend to `{{instance}} read` and `{{instance}} write`.

---

## Module 3 — Workload Health

---

### 3.1 — Pod restarts

**View**: Table | **Mode**: Code

**Total restarts per container (all time):**
```promql
kube_pod_container_status_restarts_total > 3
```

**Restarts in the last 15 minutes only (recent crashes):**
```promql
increase(kube_pod_container_status_restarts_total[$__rate_interval]) > 0
```

**Bonus — sort by most restarts:**
```promql
sort_desc(kube_pod_container_status_restarts_total)
```

---

### 3.2 — OOMKilled containers

**View**: Table | **Mode**: Code

```promql
kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}
```

**Count of OOMKills per namespace:**
```promql
count by (namespace) (
  kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}
)
```

**Bonus — all termination reasons (OOMKilled, Error, Completed):**
```promql
count by (reason) (kube_pod_container_status_last_terminated_reason)
```

---

### 3.3 — Pending pods

**View**: Table | **Mode**: Code

**All pods currently in Pending phase:**
```promql
kube_pod_status_phase{phase="Pending"} == 1
```

**How long has each pending pod been waiting (in minutes):**
```promql
(time() - kube_pod_created) / 60
* on(pod, namespace) group_left()
  (kube_pod_status_phase{phase="Pending"} == 1)
```

---

### 3.4 — CPU throttling

**View**: Graph | **Mode**: Code

**Throttle % per container — high values mean the CPU limit is too tight:**
```promql
sum by (namespace, pod, container) (
  rate(container_cpu_cfs_throttled_periods_total{container!=""}[$__rate_interval])
) /
sum by (namespace, pod, container) (
  rate(container_cpu_cfs_periods_total{container!=""}[$__rate_interval])
) * 100 > 25
```

The `> 25` filters to only containers throttled more than 25% of the time.
Remove it to see all containers.

---

### 3.5 — Memory usage vs limit %

**View**: Table | **Mode**: Code

```promql
sum by (namespace, pod, container) (container_memory_working_set_bytes{container!=""})
/
sum by (namespace, pod, container) (
  kube_pod_container_resource_limits{resource="memory", container!=""}
) * 100
```

**Flag containers above 80% of their memory limit:**
```promql
(
  sum by (namespace, pod, container) (container_memory_working_set_bytes{container!=""})
  /
  sum by (namespace, pod, container) (
    kube_pod_container_resource_limits{resource="memory", container!=""}
  ) * 100
) > 80
```

**Why `working_set_bytes` not `rss`**: `working_set_bytes` includes file-backed
memory the kernel cannot reclaim — this is what the OOM killer uses to decide
what to kill.

---

### 3.6 — Deployment replica drift

**View**: Table | **Mode**: Code

**Deployments where desired ≠ ready:**
```promql
(kube_deployment_spec_replicas - kube_deployment_status_replicas_ready) > 0
```

**All deployments with ready/desired counts:**
```promql
kube_deployment_spec_replicas
* on(namespace, deployment) group_left()
  (kube_deployment_status_replicas_ready / kube_deployment_spec_replicas)
```

**Simpler — just show ready vs desired side by side (two queries):**
```promql
# Query A
kube_deployment_spec_replicas

# Query B
kube_deployment_status_replicas_ready
```

---

## Module 4 — Control Plane

---

### 4.1 — API server request rate by verb

**View**: Graph | **Mode**: Code

```promql
sum by (verb) (
  rate(apiserver_request_total{job="apiserver"}[$__rate_interval])
)
```

**Add resource type breakdown:**
```promql
sum by (verb, resource) (
  rate(apiserver_request_total{job="apiserver"}[$__rate_interval])
)
```

---

### 4.2 — API server error rate

**View**: Graph | **Mode**: Code

**5xx error rate as % of total requests:**
```promql
sum(rate(apiserver_request_total{job="apiserver", code=~"5.."}[$__rate_interval]))
/
sum(rate(apiserver_request_total{job="apiserver"}[$__rate_interval])) * 100
```

**Alert expression — fires when error rate > 1%:**
```promql
(
  sum(rate(apiserver_request_total{job="apiserver", code=~"5.."}[$__rate_interval]))
  /
  sum(rate(apiserver_request_total{job="apiserver"}[$__rate_interval]))
) * 100 > 1
```

---

### 4.3 — API server latency p50 / p90 / p99

**View**: Graph | **Mode**: Code — add 3 queries

```promql
# p50
histogram_quantile(0.50, sum by (le, verb) (
  rate(apiserver_request_duration_seconds_bucket{job="apiserver"}[$__rate_interval])
))

# p90
histogram_quantile(0.90, sum by (le, verb) (
  rate(apiserver_request_duration_seconds_bucket{job="apiserver"}[$__rate_interval])
))

# p99
histogram_quantile(0.99, sum by (le, verb) (
  rate(apiserver_request_duration_seconds_bucket{job="apiserver"}[$__rate_interval])
))
```

`LIST` and `WATCH` verbs are typically the slowest — they involve full resource
scans. `GET` should be sub-millisecond.

---

### 4.4 — etcd round-trip as seen by API server

**View**: Graph | **Mode**: Code

```promql
histogram_quantile(0.99, sum by (le, operation) (
  rate(etcd_request_duration_seconds_bucket[$__rate_interval])
))
```

This measures the full round-trip from the API server's perspective — includes
network + etcd processing. Compare with `etcd_disk_wal_fsync_duration_seconds`
which is measured inside etcd itself.

---

### 4.5 — Scheduler pending pods by queue

**View**: Graph | **Mode**: Code

```promql
scheduler_pending_pods
```

The `queue` label has three values:

| Queue | Meaning |
|---|---|
| `active` | pods being actively scheduled right now |
| `backoff` | pods that failed scheduling, waiting to retry |
| `unschedulable` | pods that couldn't be scheduled (no node fits) |

`unschedulable > 0` for sustained periods = node capacity or taints issue.

---

### 4.6 — Controller manager work queue depth

**View**: Table | **Mode**: Code

**Which controller has the deepest queue right now:**
```promql
sort_desc(workqueue_depth{job="kube-controller-manager"})
```

**Queue add rate per controller (how fast work is coming in):**
```promql
sort_desc(
  sum by (name) (
    rate(workqueue_adds_total{job="kube-controller-manager"}[$__rate_interval])
  )
)
```

**Processing latency p99 per controller:**
```promql
histogram_quantile(0.99, sum by (le, name) (
  rate(workqueue_queue_duration_seconds_bucket{job="kube-controller-manager"}[$__rate_interval])
))
```

---

## Module 5 — etcd Deep Dive

---

### 5.1 — DB size as % of quota per member

**View**: Graph | **Mode**: Code

```promql
(
  etcd_mvcc_db_total_size_in_bytes
  / etcd_server_quota_backend_bytes
) * 100
```

**Fragmentation — reclaimable space per member:**
```promql
etcd_mvcc_db_total_size_in_bytes - etcd_mvcc_db_total_size_in_use_in_bytes
```

**Add both as two queries** — when fragmentation is large relative to total,
the compact + defrag procedure from the runbook will help.

---

### 5.2 — Identify the current leader

**View**: Table | **Mode**: Code

```promql
etcd_server_is_leader == 1
```

The result will show a single series with `instance` label pointing to the
current leader's endpoint. All other members return no result (they are 0,
filtered out by `== 1`).

**Bonus — has any member lost sight of the leader:**
```promql
etcd_server_has_leader == 0
```

This returning any result means total cluster outage.

---

### 5.3 — WAL fsync and backend commit latency

**View**: Graph | **Mode**: Code — add both queries

```promql
# WAL fsync p99 (healthy: < 10 ms)
histogram_quantile(0.99, sum by (le, instance) (
  rate(etcd_disk_wal_fsync_duration_seconds_bucket[$__rate_interval])
)) * 1000

# Backend commit p99 (healthy: < 25 ms)
histogram_quantile(0.99, sum by (le, instance) (
  rate(etcd_disk_backend_commit_duration_seconds_bucket[$__rate_interval])
)) * 1000
```

`* 1000` converts seconds → milliseconds. Set panel unit to `ms` in Grafana
for automatic formatting.

If WAL p99 is high → disk is the bottleneck → run `iostat -x 1` on the node.

---

### 5.4 — Proposal health dashboard

**View**: Table for proposals_pending and gap; Graph for rates

```promql
# Failed proposals per second (should be 0)
rate(etcd_server_proposals_failed_total[$__rate_interval])

# Pending proposals (gauge — snapshot value, not rate)
etcd_server_proposals_pending

# Committed vs applied gap (both are gauges in v3.4)
etcd_server_proposals_committed_total - etcd_server_proposals_applied_total
```

A growing gap between committed and applied means etcd is receiving writes
faster than it can apply them — typically a sign of disk or CPU saturation.

---

### 5.5 — Key churn rate

**View**: Graph | **Mode**: Code

```promql
# Write rate (puts per second)
rate(etcd_mvcc_put_total[$__rate_interval])

# Delete rate (deletes per second)
rate(etcd_mvcc_delete_total[$__rate_interval])

# Total keys currently stored
etcd_debugging_mvcc_keys_total
```

High put rate with stable key count = churn (controllers reconciling frequently).
High put rate with rising key count = accumulation (something not cleaning up).

---

## Module 6 — Alert Rules

Write these as PrometheusRule YAML. Apply with:
```bash
kubectl apply -f alert-rules.yaml
```

---

### 6.1 — Node memory > 85%

```yaml
- alert: NodeMemoryHigh
  expr: |
    (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100 > 85
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Node {{ $labels.instance }} memory usage above 85%"
    description: "Memory usage is {{ $value | humanize }}% on {{ $labels.instance }}"
```

---

### 6.2 — Pod crash looping

```yaml
- alert: PodCrashLooping
  expr: |
    increase(kube_pod_container_status_restarts_total[1h]) > 5
  for: 0m
  labels:
    severity: critical
  annotations:
    summary: "Pod {{ $labels.namespace }}/{{ $labels.pod }} is crash looping"
    description: "Container {{ $labels.container }} restarted {{ $value }} times in 1h"
```

---

### 6.3 — etcd WAL fsync slow

```yaml
- alert: EtcdWALFsyncSlow
  expr: |
    histogram_quantile(0.99,
      sum by (le, instance) (
        rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])
      )
    ) > 0.01
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "etcd WAL fsync p99 above 10ms on {{ $labels.instance }}"
    description: "WAL fsync p99 is {{ $value | humanizeDuration }} — check disk I/O"
```

---

### 6.4 — API server error rate > 1%

```yaml
- alert: APIServerHighErrorRate
  expr: |
    (
      sum(rate(apiserver_request_total{code=~"5.."}[5m]))
      /
      sum(rate(apiserver_request_total[5m]))
    ) * 100 > 1
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "API server 5xx error rate above 1%"
    description: "Error rate is {{ $value | humanize }}%"
```

---

### 6.5 — Deployment under-replicated

```yaml
- alert: DeploymentReplicaMismatch
  expr: |
    (kube_deployment_spec_replicas - kube_deployment_status_replicas_ready) > 0
  for: 3m
  labels:
    severity: warning
  annotations:
    summary: "Deployment {{ $labels.namespace }}/{{ $labels.deployment }} has fewer ready replicas than desired"
    description: "{{ $value }} replica(s) missing"
```

---

## Module 7 — Advanced Joins

---

### 7.1 — Pod CPU usage enriched with node name

**View**: Table | **Mode**: Code

`container_cpu_usage_seconds_total` has no `node` label — join it from
`kube_pod_info`:

```promql
sum by (pod, namespace, node) (
  rate(container_cpu_usage_seconds_total{container!=""}[$__rate_interval])
  * on(namespace, pod) group_left(node)
  max by(namespace, pod, node) (kube_pod_info)
)
```

`group_left(node)` means: keep all left-side series and carry the `node` label
across from the right side.

---

### 7.2 — CPU and memory cost attribution by namespace

**View**: Table | **Mode**: Code

```promql
# CPU cores per namespace
sort_desc(
  sum by (namespace) (
    rate(container_cpu_usage_seconds_total{container!=""}[$__rate_interval])
  )
)

# Memory GB per namespace
sort_desc(
  sum by (namespace) (
    container_memory_working_set_bytes{container!=""}
  ) / 1073741824
)
```

---

### 7.3 — Noisy neighbour — pods on worker-01 only

**View**: Table | **Mode**: Code

```promql
sum by (pod, namespace) (
  rate(container_cpu_usage_seconds_total{container!=""}[$__rate_interval])
  * on(namespace, pod) group_left(node)
    max by(namespace, pod, node) (kube_pod_info{node="worker-01"})
)
```

Only pods scheduled on `worker-01` appear. Sorted descending to find the
CPU hog:

```promql
sort_desc(
  sum by (pod, namespace) (
    rate(container_cpu_usage_seconds_total{container!=""}[$__rate_interval])
    * on(namespace, pod) group_left(node)
      max by(namespace, pod, node) (kube_pod_info{node="worker-01"})
  )
)
```

---

### 7.4 — etcd key write rate by Kubernetes resource type

**View**: Graph | **Mode**: Code

etcd keys follow the pattern `/registry/<resource>/<namespace>/<name>`.
The `etcd_mvcc_put_total` metric does not carry the key path, but you can
correlate by watching the API server request rate by resource — high write
rate to a resource = high etcd churn for that resource:

```promql
sort_desc(
  sum by (resource, verb) (
    rate(apiserver_request_total{verb=~"CREATE|UPDATE|PATCH|DELETE"}[$__rate_interval])
  )
)
```

Resources with the highest write verb rate are driving the most etcd churn.

---

### 7.5 — API server SLO: 99% of requests under 1 second

**View**: Graph | **Mode**: Code

**Compliance rate (target: > 0.99):**
```promql
sum(rate(apiserver_request_duration_seconds_bucket{le="1"}[$__rate_interval]))
/
sum(rate(apiserver_request_duration_seconds_count[$__rate_interval]))
```

**Error budget remaining (starts at 1, burns toward 0):**
```promql
1 - (
  1 -
  sum(rate(apiserver_request_duration_seconds_bucket{le="1"}[$__rate_interval]))
  /
  sum(rate(apiserver_request_duration_seconds_count[$__rate_interval]))
) / (1 - 0.99)
```

**Burn rate over 1 hour (alert if > 1 — budget burning faster than replenished):**
```promql
(
  1 -
  sum(rate(apiserver_request_duration_seconds_bucket{le="1"}[1h]))
  /
  sum(rate(apiserver_request_duration_seconds_count[1h]))
) / (1 - 0.99)
```

A burn rate of `1.0` means you are consuming error budget at exactly the rate
it replenishes. `> 2.0` means you will exhaust the monthly budget in < 15 days.

---

## Quick Reference — Most Useful Queries

```promql
# Node CPU %
(1 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval]))) * 100

# Node memory %
(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100

# Pod restarts last 15m
increase(kube_pod_container_status_restarts_total[15m]) > 0

# CPU throttle % per container
rate(container_cpu_cfs_throttled_periods_total{container!=""}[$__rate_interval])
/ rate(container_cpu_cfs_periods_total{container!=""}[$__rate_interval]) * 100

# Deployments not fully ready
(kube_deployment_spec_replicas - kube_deployment_status_replicas_ready) > 0

# API server p99 latency
histogram_quantile(0.99, sum by(le)(rate(apiserver_request_duration_seconds_bucket[$__rate_interval])))

# etcd leader
etcd_server_is_leader == 1

# etcd DB usage %
etcd_mvcc_db_total_size_in_bytes / etcd_server_quota_backend_bytes * 100

# etcd WAL fsync p99 (ms)
histogram_quantile(0.99, sum by(le,instance)(rate(etcd_disk_wal_fsync_duration_seconds_bucket[$__rate_interval]))) * 1000
```
