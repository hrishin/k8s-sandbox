import pulumi
import pulumi_random as random
import pulumiverse_scaleway as scaleway

import scripts


def _ipv4(server: scaleway.InstanceServer) -> pulumi.Output:
    return server.public_ips.apply(
        lambda ips: next(ip["address"] for ip in ips if ip.get("family") == "inet")
    )


# ── Config ────────────────────────────────────────────────────────────────────
config = pulumi.Config()
zone = config.get("zone") or "fr-par-2"
instance_type = config.get("instanceType") or "PLAY2-MICRO"
control_plane_count = config.get_int("controlPlaneCount") or 1
worker_count = config.get_int("workerCount") or 1
k8s_version = config.get("k8sVersion") or "1.34"
lb_type = config.get("lbType") or "LB-S"

# ── Kubeadm join token (persistent across runs via Pulumi state) ───────────────
token_id = random.RandomString(
    "token-id", length=6, special=False, upper=False, numeric=True
)
token_secret = random.RandomString(
    "token-secret", length=16, special=False, upper=False, numeric=True
)
join_token = pulumi.Output.concat(token_id.result, ".", token_secret.result)

# ── Certificate key for HA control plane (32 bytes = 64-char hex) ─────────────
# kubeadm uses this to encrypt/decrypt certs uploaded to the cluster secret
cert_key_random = random.RandomId("cert-key", byte_length=32)
cert_key = cert_key_random.hex

# ── Control plane servers ──────────────────────────────────────────────────────
control_planes = []
for i in range(control_plane_count):
    cp_name = f"control-plane-{i + 1:02d}"
    cp = scaleway.InstanceServer(
        cp_name,
        zone=zone,
        name=cp_name,
        type=instance_type,
        image="ubuntu_jammy",
        enable_dynamic_ip=True,
    )
    control_planes.append((cp_name, cp))

first_cp_name, first_cp = control_planes[0]
first_cp_ip = _ipv4(first_cp)

# ── Load balancer (fronts all control plane API servers on port 6443) ──────────
lb_ip_resource = scaleway.LoadbalancerIp("k8s-lb-ip", zone=zone)

lb = scaleway.Loadbalancer(
    "k8s-lb",
    name="k8s-control-plane",
    zone=zone,
    type=lb_type,
    ip_ids=[lb_ip_resource.id],
)

lb_ip = lb_ip_resource.ip_address

lb_backend = scaleway.LoadbalancerBackend(
    "k8s-lb-backend",
    lb_id=lb.id,
    name="k8s-api-server",
    forward_protocol="tcp",
    forward_port=6443,
    server_ips=pulumi.Output.all(*[_ipv4(cp) for _, cp in control_planes]),
    health_check_port=6443,
    health_check_tcp=scaleway.LoadbalancerBackendHealthCheckTcpArgs(),
)

scaleway.LoadbalancerFrontend(
    "k8s-lb-frontend",
    lb_id=lb.id,
    backend_id=lb_backend.id,
    name="k8s-api-server",
    inbound_port=6443,
)

# ── Worker servers ─────────────────────────────────────────────────────────────
workers = []
for i in range(worker_count):
    worker_name = f"worker-{i + 1:02d}"
    worker = scaleway.InstanceServer(
        worker_name,
        zone=zone,
        name=worker_name,
        type=instance_type,
        image="ubuntu_jammy",
        enable_dynamic_ip=True,
    )
    workers.append((worker_name, worker))

# ── Setup scripts (run via SSH after servers are provisioned) ─────────────────
cp_scripts = {}

# First control plane: runs kubeadm init (endpoint = LB, SANs include LB + node IP)
first_cp_script = pulumi.Output.all(
    token=join_token,
    lb=lb_ip,
    node=first_cp_ip,
    cert=cert_key,
).apply(lambda a: scripts.first_control_plane(a["token"], a["lb"], a["node"], k8s_version, a["cert"], first_cp_name))
cp_scripts[first_cp_name] = first_cp_script

# Additional control planes: join via LB with --control-plane
for cp_name, _ in control_planes[1:]:
    script = pulumi.Output.all(
        token=join_token,
        lb=lb_ip,
        cert=cert_key,
    ).apply(lambda a, n=cp_name: scripts.additional_control_plane(a["token"], a["lb"], k8s_version, a["cert"], n))
    cp_scripts[cp_name] = script

# Workers: join via LB
worker_scripts = {}
for worker_name, _ in workers:
    script = pulumi.Output.all(
        token=join_token,
        lb=lb_ip,
    ).apply(lambda a, wn=worker_name: scripts.worker(a["token"], a["lb"], k8s_version, wn))
    worker_scripts[worker_name] = script

# ── Exports ───────────────────────────────────────────────────────────────────
pulumi.export("load_balancer_ip", lb_ip)
pulumi.export("control_plane_ips", {cp_name: _ipv4(cp) for cp_name, cp in control_planes})
pulumi.export("control_plane_ids", {cp_name: cp.id for cp_name, cp in control_planes})
pulumi.export("worker_ips", {worker_name: _ipv4(w) for worker_name, w in workers})
pulumi.export("worker_ids", {worker_name: w.id for worker_name, w in workers})
pulumi.export("zone", zone)
pulumi.export("k8s_version", k8s_version)
pulumi.export("join_token", join_token)
pulumi.export("control_plane_setup_commands", cp_scripts)
pulumi.export("worker_setup_commands", worker_scripts)
