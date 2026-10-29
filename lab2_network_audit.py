#!/usr/bin/env python3
"""
Лабораторна робота 2: Автоматизація мережевих задач.
Інвентаризація та перевірка контейнерної мережі через Docker CLI + Docker Engine API.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import docker
import paramiko
import requests

# ---------- Конфігурація (читається зі змінних середовища) ----------
SSH_HOST = os.getenv("LAB_SSH_HOST", "localhost")
SSH_PORT = int(os.getenv("LAB_SSH_PORT", "22"))
SSH_USER = os.getenv("LAB_SSH_USER", "olyalya")
SSH_KEY_PATH = os.getenv("LAB_SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519"))
PROJECT_NAME = os.getenv("LAB_PROJECT", "lab1")
PROXY_URL = os.getenv("LAB_PROXY_URL", "http://localhost:8080")
PROXY_SERVICE_NAME = os.getenv("LAB_PROXY_SERVICE", "proxy")
APP_SERVICE_NAME = os.getenv("LAB_APP_SERVICE", "app")
DB_SERVICE_NAME = os.getenv("LAB_DB_SERVICE", "db")


# ==================== 1. SSH + Docker CLI ====================

def ssh_exec(client: paramiko.SSHClient, cmd: str, timeout: int = 15) -> tuple[str, str, int]:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return (
        stdout.read().decode("utf-8", errors="replace").strip(),
        stderr.read().decode("utf-8", errors="replace").strip(),
        exit_code,
    )


def collect_via_cli(ssh: paramiko.SSHClient, project: str) -> list[dict]:
    out, err, rc = ssh_exec(
        ssh,
        f"docker ps -a --no-trunc --filter label=com.docker.compose.project={project} "
        f"--format '{{{{.ID}}}}|{{{{.Image}}}}|{{{{.State}}}}'",
    )
    if rc != 0:
        raise RuntimeError(f"docker ps failed: {err}")

    containers = []
    for line in out.splitlines():
        if not line.strip():
            continue
        cid, image, state = [p.strip() for p in line.split("|", 2)]

        out2, _, rc2 = ssh_exec(
            ssh,
            f"docker inspect --format "
            f"'{{{{index .Config.Labels \"com.docker.compose.service\"}}}}|{{{{.State.StartedAt}}}}' "
            f"{cid}",
        )
        if rc2 != 0:
            service, started = "unknown", ""
        else:
            parts = out2.split("|", 1)
            service = parts[0].strip() if parts else "unknown"
            started = parts[1].strip() if len(parts) > 1 else ""

        out3, _, _ = ssh_exec(
            ssh,
            f"docker inspect --format "
            f"'{{{{range $name, $net := .NetworkSettings.Networks}}}}{{{{$name}}}}:{{{{$net.IPAddress}}}};{{{{end}}}}' "
            f"{cid}",
        )
        networks = {}
        for pair in out3.split(";"):
            if ":" in pair:
                net_name, ip = pair.split(":", 1)
                networks[net_name.strip()] = ip.strip()

        out4, _, _ = ssh_exec(ssh, f"docker port {cid}")
        ports = [ln.strip() for ln in out4.splitlines() if ln.strip()]

        containers.append({
            "id": cid,
            "service": service,
            "image": image,
            "state": state,
            "started_at": started,
            "networks": networks,
            "ports": sorted(ports),
        })
    return containers


# ==================== 2. Docker SDK (Engine API) ====================

def collect_via_api(project: str) -> list[dict]:
    client = docker.DockerClient(
        base_url=f"ssh://{SSH_USER}@{SSH_HOST}:{SSH_PORT}",
        version="auto",
        timeout=60,
    )
    try:
        raw = client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={project}"},
        )
        containers = []
        for c in raw:
            attrs = c.attrs
            networks = {}
            for net_name, net_data in attrs.get("NetworkSettings", {}).get("Networks", {}).items():
                networks[net_name] = net_data.get("IPAddress", "")

            ports = []
            ports_map = attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
            for container_port, bindings in ports_map.items():
                if not bindings:
                    continue
                for b in bindings:
                    host_ip = b.get("HostIp", "0.0.0.0")
                    host_port = b.get("HostPort", "")
                    if host_port:
                        ports.append(f"{host_ip}:{host_port}->{container_port}")
            ports = sorted(set(ports))

            containers.append({
                "id": c.id,
                "service": c.labels.get("com.docker.compose.service", "unknown"),
                "image": attrs.get("Config", {}).get("Image", ""),
                "state": attrs.get("State", {}).get("Status", ""),
                "started_at": attrs.get("State", {}).get("StartedAt", ""),
                "networks": networks,
                "ports": ports,
            })
        return containers
    finally:
        client.close()


# ==================== 3. Перевірка мережевої конфігурації ====================

def check_network_config(containers: list[dict]) -> tuple[list[str], list[str]]:
    net_map: dict[str, list[str]] = {}
    for c in containers:
        for net in c["networks"]:
            net_map.setdefault(net, []).append(c["service"])
    table = [f"{net} -> {sorted(svcs)}" for net, svcs in sorted(net_map.items())]

    violations = []
    for c in containers:
        svc = c["service"]
        if svc == PROXY_SERVICE_NAME:
            continue
        if c["ports"]:
            violations.append(f"{svc} ({c['id'][:12]}) має опубліковані порти: {c['ports']}")

    proxy_containers = [c for c in containers if c["service"] == PROXY_SERVICE_NAME]
    if proxy_containers and not any(c["ports"] for c in proxy_containers):
        violations.append(f"Сервіс '{PROXY_SERVICE_NAME}' не має жодного опублікованого порту")

    return table, violations


# ==================== 4. HTTP-перевірка ====================

def http_check(url: str, attempts: int = 2) -> list[dict]:
    results = []
    for i in range(attempts):
        try:
            resp = requests.get(url, timeout=5)
            results.append({
                "attempt": i + 1,
                "status_code": resp.status_code,
                "elapsed_sec": round(resp.elapsed.total_seconds(), 3),
                "body_snippet": resp.text[:200],
                "error": None,
            })
        except requests.RequestException as e:
            results.append({
                "attempt": i + 1,
                "status_code": None,
                "elapsed_sec": None,
                "body_snippet": "",
                "error": str(e),
            })
    return results


# ==================== 5. Нормалізація та порівняння ====================

def _canon_ports(ports: list[str]) -> list[str]:
    """
    Привести порти до канонічного вигляду: 'container_port->host_ip:host_port'.
    Приймає як CLI-формат ('80/tcp -> 0.0.0.0:8080', '80/tcp -> [::]:8080'),
    так і API-формат ('0.0.0.0:8080->80/tcp', ':::8080->80/tcp').
    """
    canon = []
    for p in ports:
        s = p.replace(" ", "")
        if "->" not in s:
            continue
        left, right = s.split("->", 1)

        # Визначити, де контейнерний порт (містить '/'), а де host-прив'язка
        if "/" in left and "/" not in right:
            container_port, host_binding = left, right
        elif "/" in right and "/" not in left:
            container_port, host_binding = right, left
        else:
            container_port, host_binding = left, right

        # Нормалізувати IPv6
        host_binding = host_binding.replace("[::]", "::")
        if host_binding.startswith(":::"):
            host_ip, host_port = "::", host_binding[3:]
        elif ":" in host_binding:
            host_ip, host_port = host_binding.rsplit(":", 1)
        else:
            host_ip, host_port = "", host_binding

        canon.append(f"{container_port}->{host_ip}:{host_port}")
    return sorted(set(canon))


def normalize(containers: list[dict]) -> dict[str, dict]:
    return {
        c["id"]: {
            "id": c["id"],
            "service": c["service"],
            "image": c["image"],
            "state": c["state"],
            "networks": dict(sorted(c["networks"].items())),
            "ports": _canon_ports(c["ports"]),
        }
        for c in containers
    }


def compare_cli_api(cli_list: list[dict], api_list: list[dict]) -> list[str]:
    cli, api = normalize(cli_list), normalize(api_list)
    diffs = []
    for cid in set(cli) - set(api):
        diffs.append(f"ID {cid[:12]}: присутній у CLI, відсутній у API")
    for cid in set(api) - set(cli):
        diffs.append(f"ID {cid[:12]}: присутній у API, відсутній у CLI")
    for cid in set(cli) & set(api):
        a, b = cli[cid], api[cid]
        short = cid[:12]
        for field in ("service", "state", "networks", "ports"):
            if a[field] != b[field]:
                diffs.append(f"{short}: {field} CLI={a[field]} API={b[field]}")
    return diffs


# ==================== 6. Збереження результатів ====================

def save_inventory(host: str, project: str, cli_data: list[dict], api_data: list[dict],
                   path: str = "inventory.json") -> None:
    payload = {
        "host": host,
        "project": project,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "cli": cli_data,
        "api": api_data,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def save_report(rows: list[list], path: str = "report.csv") -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["check", "result", "details"])
        for row in rows:
            w.writerow(row)


# ==================== 7. Моніторинг ====================

def run_monitoring(interval: int, duration: int | None = None) -> None:
    client = docker.DockerClient(
        base_url=f"ssh://{SSH_USER}@{SSH_HOST}:{SSH_PORT}",
        version="auto",
        timeout=30,
    )
    rows = []
    start = time.time()
    try:
        while True:
            ts = datetime.now(timezone.utc).isoformat()
            states = {}
            try:
                for c in client.containers.list(
                    all=True,
                    filters={"label": f"com.docker.compose.project={PROJECT_NAME}"},
                ):
                    svc = c.labels.get("com.docker.compose.service", "unknown")
                    states[svc] = c.attrs.get("State", {}).get("Status", "unknown")
            except Exception as e:
                states = {"error": str(e)}
            try:
                r = requests.get(PROXY_URL, timeout=5)
                http_status, http_time = r.status_code, round(r.elapsed.total_seconds(), 3)
            except requests.RequestException:
                http_status, http_time = "FAIL", ""

            rows.append([
                ts,
                states.get(APP_SERVICE_NAME, "N/A"),
                states.get(DB_SERVICE_NAME, "N/A"),
                states.get(PROXY_SERVICE_NAME, "N/A"),
                http_status,
                http_time,
            ])
            print(f"[{ts}] app={states.get(APP_SERVICE_NAME)} db={states.get(DB_SERVICE_NAME)} "
                  f"proxy={states.get(PROXY_SERVICE_NAME)} http={http_status}")

            if duration and (time.time() - start) >= duration:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nМоніторинг зупинено користувачем.")
    finally:
        client.close()
        with open("monitoring.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "app_state", "db_state", "proxy_state",
                        "http_status", "http_elapsed_sec"])
            w.writerows(rows)
        print(f"Записано {len(rows)} рядків у monitoring.csv")


# ==================== main ====================

def main():
    parser = argparse.ArgumentParser(description="Lab 2: Docker network audit via SSH")
    parser.add_argument("--monitor", action="store_true", help="Увімкнути періодичний моніторинг")
    parser.add_argument("--interval", type=int, default=10, help="Інтервал моніторингу, сек")
    parser.add_argument("--duration", type=int, default=None, help="Тривалість моніторингу, сек")
    args = parser.parse_args()

    if args.monitor:
        run_monitoring(args.interval, args.duration)
        return

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            hostname=SSH_HOST, port=SSH_PORT, username=SSH_USER,
            key_filename=SSH_KEY_PATH, timeout=10,
        )
    except Exception as e:
        print(f"SSH connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    out, err, rc = ssh_exec(ssh, "docker info --format '{{.ServerVersion}}'")
    if rc != 0:
        print(f"Docker недоступний: {err}", file=sys.stderr)
        ssh.close()
        sys.exit(1)
    print(f"SSH OK, Docker Engine version: {out}")

    print("Збір через Docker CLI...")
    cli_data = collect_via_cli(ssh, PROJECT_NAME)
    ssh.close()

    print("Збір через Docker SDK (Engine API)...")
    api_data = collect_via_api(PROJECT_NAME)

    net_table, violations = check_network_config(api_data)
    print("\nМережі:")
    for line in net_table:
        print("  ", line)
    if violations:
        print("Порушення конфігурації портів:")
        for v in violations:
            print("  -", v)
    else:
        print("Конфігурація портів: відповідає вимогам")

    print("\nHTTP-перевірка через проксі...")
    http_results = http_check(PROXY_URL)
    for r in http_results:
        print(f"  attempt={r['attempt']} status={r['status_code']} "
              f"elapsed={r['elapsed_sec']}s error={r['error']}")
        print(f"    body: {r['body_snippet'][:80]}")

    diffs = compare_cli_api(cli_data, api_data)
    print("\nПорівняння CLI vs API:")
    if diffs:
        for d in diffs:
            print("  DIFF:", d)
    else:
        print("  Відмінностей не виявлено (контейнери зіставлено за повним ID).")

    save_inventory(SSH_HOST, PROJECT_NAME, cli_data, api_data)
    rows = [
        ["ssh_docker_info", "OK", f"Docker version {out}"],
        ["cli_containers_count", len(cli_data), f"project={PROJECT_NAME}"],
        ["api_containers_count", len(api_data), f"project={PROJECT_NAME}"],
        ["network_table", "OK", "; ".join(net_table)],
        ["port_publishing_check", "OK" if not violations else "FAIL",
         "; ".join(violations) or "лише proxy публікує порти"],
    ]
    for r in http_results:
        rows.append([f"http_attempt_{r['attempt']}", r["status_code"] or "FAIL",
                     f"elapsed={r['elapsed_sec']}s error={r['error']}"])
    rows.append(["cli_vs_api_diff", "IDENTICAL" if not diffs else "DIFFERENT",
                 "; ".join(diffs) or "усі контейнери збігаються"])
    save_report(rows)
    print("\nФайли збережено: inventory.json, report.csv")


if __name__ == "__main__":
    main()
