def first_control_plane(
    join_token: str, lb_ip: str, node_ip: str, k8s_version: str, cert_key: str, cp_name: str
) -> str:
    return f"bash -s -- {join_token} {lb_ip} {node_ip} {k8s_version} {cert_key} {cp_name}"


def additional_control_plane(
    join_token: str, lb_ip: str, k8s_version: str, cert_key: str, cp_name: str
) -> str:
    return f"bash -s -- {join_token} {lb_ip} {k8s_version} {cert_key} {cp_name}"


def worker(join_token: str, lb_ip: str, k8s_version: str, worker_name: str) -> str:
    return f"bash -s -- {join_token} {lb_ip} {k8s_version} {worker_name}"
