#!/usr/bin/env python3
"""
BIG-IP Next for Kubernetes — ROKS Cluster Schematics Lifecycle Test

Manages a single IBM Schematics workspace for the roks_cluster_4_18 Terraform module.

Phases (preflight and setup always run):
  create   — create the Schematics workspace
  plan     — plan (validate) the workspace
  apply    — apply (provision) the workspace
  destroy  — destroy (deprovision) the workspace
  delete   — delete the workspace record from Schematics

Usage:
    python3 test_lifecycle.py [path/to/terraform.tfvars] [options]

    --branch BRANCH     GitHub branch to deploy (default: main)
    --phases PHASE ...  Phases to run (default: all)
    --ws-id WS_ID       Existing workspace ID (required when create is not in --phases)
    --list              List workspaces matching this repo's name prefix and exit
    --resources         Print workspace resource list and exit
    --outputs           Print workspace output variables and exit

Prerequisites:
    ibmcloud CLI installed and authenticated:
        ibmcloud login --apikey YOUR_API_KEY -r REGION
    Schematics plugin:
        ibmcloud plugin install schematics
"""

import json
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

REPO_URL       = "https://github.com/f5devcentral/ibmcloud_schematics_bigip_next_for_kubernetes_roks_cluster_4"
WS_NAME_PREFIX = "bnk-23-roks-cluster"
TITLE          = "BIG-IP Next for Kubernetes — ROKS Cluster"
TFVARS_DEFAULT = "terraform.tfvars"
WS_JSON_PATH   = "workspace.json"
REPORT_DIR     = Path("test-reports")

POLL_INTERVAL = 30
JOB_TIMEOUT   = 18000   # 300 min — cluster creation can take ~47 min
READY_TIMEOUT = 300

SECURE_VARS       = {"ibmcloud_api_key", "bigip_password"}
TERMINAL_STATUSES = {"INACTIVE", "ACTIVE", "FAILED", "STOPPED", "DRAFT"}

VALID_PHASES = ["create", "plan", "apply", "destroy", "delete"]

KEY_OUTPUTS = [
    "openshift_cluster_name",
    "openshift_cluster_public_endpoint",
    "openshift_cluster_private_endpoint",
    "openshift_cluster_ingress_hostname",
    "openshift_cluster_state",
    "openshift_version_used",
    "roks_cluster_vpc_name",
    "roks_transit_gateway_name",
]


# ── Low-level helpers ─────────────────────────────────────────────────────────

def tee(msg, lf=None):
    print(msg, flush=True)
    if lf:
        print(msg, file=lf, flush=True)


def run_cmd(cmd, lf=None, stream=False):
    if not stream:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    buf = []
    for line in proc.stdout:
        print(line, end="", flush=True)
        if lf:
            print(line, end="", file=lf, flush=True)
        buf.append(line)
    proc.wait()
    return proc.returncode, "".join(buf), ""


def ibmcloud_json(cmd, lf=None):
    rc, out, err = run_cmd(f"{cmd} --output json")
    if lf and out.strip():
        print(out, file=lf, flush=True)
    if rc != 0:
        raise RuntimeError(f"Command failed: {cmd}\n{(err or out).strip()}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON output from: {cmd}\n{out}") from exc


# ── tfvars / workspace.json ───────────────────────────────────────────────────

def parse_tfvars(path):
    variables = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^(\w+)\s*=\s*(.+)$', line)
            if not m:
                continue
            name, raw = m.group(1), m.group(2).strip()
            if raw in ("true", "false"):
                entry = {"name": name, "value": raw, "type": "bool"}
            elif re.match(r'^-?\d+(\.\d+)?$', raw):
                entry = {"name": name, "value": raw, "type": "number"}
            else:
                entry = {"name": name, "value": raw.strip('"'), "type": "string"}
            if name in SECURE_VARS:
                entry["secure"] = True
            variables.append(entry)
    return variables


def build_workspace_json(variables, ts_label, branch="main"):
    var_map        = {v["name"]: v["value"] for v in variables}
    location       = var_map.get("ibmcloud_schematics_region", "us-south")
    resource_group = var_map.get("ibmcloud_resource_group", "default")
    ws = {
        "name": f"{WS_NAME_PREFIX}-test-{ts_label}",
        "type": ["terraform_v1.5"],
        "location": location,
        "description": f"Lifecycle test — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "resource_group": resource_group,
        "template_repo": {
            "url": REPO_URL,
            "branch": branch,
        },
        "template_data": [{
            "folder": ".",
            "type": "terraform_v1.5",
            "variablestore": variables,
        }],
    }
    Path(WS_JSON_PATH).write_text(json.dumps(ws, indent=2))
    return ws


# ── Schematics polling ────────────────────────────────────────────────────────

def get_ws_info(ws_id):
    try:
        data   = ibmcloud_json(f"ibmcloud schematics workspace get --id {ws_id}")
        status = data.get("status") or data.get("workspace_status_msg", {}).get("status_code") or "UNKNOWN"
        locked = data.get("workspace_status", {}).get("locked", False)
        return status, locked
    except Exception:
        return "UNKNOWN", True


def get_ws_status(ws_id):
    status, _ = get_ws_info(ws_id)
    return status


def wait_for_workspace_ready(ws_id, lf, timeout=READY_TIMEOUT):
    start = time.time()
    while True:
        elapsed = int(time.time() - start)
        if elapsed > timeout:
            tee(f"\n  WARNING: workspace not ready after {timeout}s — proceeding anyway", lf)
            return get_ws_status(ws_id)
        status, locked = get_ws_info(ws_id)
        if status in {"INACTIVE", "ACTIVE", "FAILED"} and not locked:
            print()
            return status
        msg = f"  [ready] {elapsed}s  status={status}  locked={locked}"
        print(f"\r{msg:<76}", end="", flush=True)
        print(msg, file=lf, flush=True)
        time.sleep(10)


def poll_until_terminal(ws_id, label, lf, timeout=JOB_TIMEOUT):
    start = time.time()
    while True:
        elapsed = int(time.time() - start)
        if elapsed > timeout:
            return "TIMEOUT", elapsed
        status = get_ws_status(ws_id)
        if status in TERMINAL_STATUSES:
            print()
            return status, elapsed
        msg = f"  [{label}] {elapsed}s elapsed  status={status}"
        print(f"\r{msg:<76}", end="", flush=True)
        print(msg, file=lf, flush=True)
        time.sleep(POLL_INTERVAL)


def stream_logs(ws_id, act_id, lf):
    run_cmd(
        f"ibmcloud schematics logs --id {ws_id} --act-id {act_id}",
        lf=lf, stream=True,
    )


def run_job(cmd, ws_id, label, lf, success_statuses, timeout=JOB_TIMEOUT):
    pre_status    = get_ws_status(ws_id)
    lock_deadline = time.time() + timeout
    attempt       = 0

    while True:
        attempt += 1
        rc, out, err = run_cmd(f"{cmd} --output json")
        combined = (out + err).lower()
        if rc == 0:
            break
        if ("409" in combined or "temporarily locked" in combined) and time.time() < lock_deadline:
            remaining = int(lock_deadline - time.time())
            tee(f"  Workspace locked (409) — retrying in 30s "
                f"(attempt {attempt}, {remaining}s remaining in budget)", lf)
            time.sleep(30)
            continue
        if out.strip():
            print(out, file=lf, flush=True)
        raise RuntimeError((err or out).strip())

    if out.strip():
        print(out, file=lf, flush=True)

    try:
        act_id = json.loads(out).get("activityid")
    except (json.JSONDecodeError, AttributeError):
        act_id = None

    tee(f"  Activity ID : {act_id or '(unavailable)'}", lf)

    t0 = time.time()
    if act_id:
        tee("  Waiting for activity to start...", lf)
        t_transition = time.time()
        while time.time() - t_transition < 120:
            if get_ws_status(ws_id) != pre_status:
                break
            time.sleep(5)

        tee("  Polling until activity completes...", lf)
        final_status, _ = poll_until_terminal(ws_id, label, lf, timeout=timeout)

        tee("  Fetching final logs...", lf)
        stream_logs(ws_id, act_id, lf)
        tee("", lf)
    else:
        tee("  No activity ID returned — polling workspace status...", lf)
        final_status, _ = poll_until_terminal(ws_id, label, lf, timeout=timeout)

    elapsed = int(time.time() - t0)
    passed  = final_status in success_statuses
    return passed, final_status, elapsed


def fetch_outputs(ws_id, lf=None):
    try:
        data  = ibmcloud_json(f"ibmcloud schematics output --id {ws_id}", lf)
        items = data if isinstance(data, list) else [data]
        out   = {}
        for template in items:
            for item in template.get("output_values", []):
                # Schematics returns each item as {output_name: {value, type, sensitive}}
                for name, meta in item.items():
                    out[name] = meta.get("value", "") if isinstance(meta, dict) else meta
        return out
    except Exception as exc:
        if lf:
            tee(f"  WARNING: could not fetch outputs: {exc}", lf)
        return {}


# ── Report rendering ──────────────────────────────────────────────────────────

class Phase:
    __slots__ = ("name", "status", "duration", "error")

    def __init__(self, name):
        self.name     = name
        self.status   = "SKIP"
        self.duration = 0
        self.error    = None


def render_report(started_at, ws_id, ws_name, phases, outputs, overall):
    elapsed = int((datetime.now(timezone.utc) - started_at).total_seconds())
    W   = 72
    sep = "=" * W
    thn = "-" * W
    lines = [
        "",
        sep,
        f"  {TITLE} — Schematics Lifecycle Test Report",
        sep,
        f"  Started     {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"  Workspace   {ws_name or 'not created'}",
        f"  WS ID       {ws_id   or 'not created'}",
        f"  Result      {overall}",
        f"  Total time  {elapsed}s  ({elapsed / 60:.1f} min)",
        thn,
        f"  {'Phase':<20} {'Result':<8} {'Duration':>10}",
        thn,
    ]
    for p in phases:
        lines.append(f"  {p.name:<20} {p.status:<8} {p.duration:>8}s")
        if p.error:
            lines.append(f"    !! {p.error}")

    if outputs:
        lines += [thn, "  Key Outputs", thn]
        printed = set()
        for key in KEY_OUTPUTS:
            val = outputs.get(key)
            if val is not None:
                lines.append(f"  {key}")
                lines.append(f"    {val}")
                printed.add(key)
        extras = {k: v for k, v in outputs.items() if k not in printed}
        if extras:
            lines.append(thn)
            for k, v in extras.items():
                lines.append(f"  {k}")
                lines.append(f"    {v}")

    lines += [sep, ""]
    return "\n".join(lines)


# ── Workspace info helpers ────────────────────────────────────────────────────

def _list_matching_workspaces():
    rc, out, err = run_cmd("ibmcloud schematics workspace list --output json")
    if rc != 0:
        return None, (err or out).strip()
    try:
        data    = json.loads(out)
        ws_list = data.get("workspaces", []) if isinstance(data, dict) else (data or [])
        matches = [
            w for w in ws_list
            if (w.get("name") or "").startswith(WS_NAME_PREFIX)
        ]
        matches.sort(key=lambda w: w.get("name", ""), reverse=True)
        return matches, None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _ws_status_str(w):
    return (
        w.get("status")
        or w.get("workspace_status_msg", {}).get("status_code")
        or "UNKNOWN"
    )


def show_workspace_list(tfvars_path):
    W   = 72
    sep = "=" * W
    thn = "─" * (W - 4)

    print(f"\n{sep}")
    print(f"  {TITLE}")
    print(f"  Workspace prefix : {WS_NAME_PREFIX}")
    if tfvars_path:
        print(f"  tfvars           : {tfvars_path}")
    print(sep)

    matches, err = _list_matching_workspaces()
    if err:
        print(f"\n  ERROR: {err}\n{sep}\n")
        return 1

    print(f"\n  {thn}")
    if not matches:
        print(f"  (no workspaces found with prefix '{WS_NAME_PREFIX}')")
    else:
        for w in matches:
            status = _ws_status_str(w)
            print(f"  {status:<12}  {w.get('name', ''):<50}  {w.get('id', '')}")
    print(f"\n{sep}\n")
    return 0


def show_resources(ws_id):
    W   = 72
    sep = "=" * W
    print(f"\n{sep}")
    print(f"  Resources  —  {ws_id}")
    print(sep)

    rc, out, err = run_cmd(f"ibmcloud schematics state list --id {ws_id}")
    if rc != 0:
        print(f"\n  ERROR: {(err or out).strip()}\n{sep}\n")
        return 1
    if out.strip():
        for line in out.strip().splitlines():
            print(f"  {line}")
    else:
        print("  (no resources)")
    print(f"\n{sep}\n")
    return 0


def show_outputs(ws_id):
    W   = 72
    sep = "=" * W
    print(f"\n{sep}")
    print(f"  Output Variables  —  {ws_id}")
    print(sep)

    outputs = fetch_outputs(ws_id)
    if not outputs:
        print("\n  (no outputs or workspace not yet applied)")
    else:
        print()
        for k, v in outputs.items():
            print(f"  {k}")
            print(f"    {v}")
    print(f"\n{sep}\n")
    return 0


def _resolve_ws_id(args_ws_id, tfvars_path, flag):
    if args_ws_id:
        return args_ws_id, None
    matches, err = _list_matching_workspaces()
    if err:
        return None, err
    if not matches:
        return None, (
            f"No workspace with prefix '{WS_NAME_PREFIX}' found.\n"
            f"       Use --ws-id WS_ID or run --list to see available workspaces."
        )
    ws_id = matches[0].get("id")
    print(f"  Auto-detected workspace: {matches[0].get('name')}  ({ws_id})")
    return ws_id, None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description=f"{TITLE} — Schematics lifecycle test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "phases (in execution order):\n"
            "  create   create the Schematics workspace\n"
            "  plan     plan (validate) the workspace\n"
            "  apply    apply (provision) the workspace\n"
            "  destroy  destroy (deprovision) the workspace\n"
            "  delete   delete the workspace record\n"
        ),
    )
    parser.add_argument(
        "tfvars", nargs="?", default=TFVARS_DEFAULT,
        help="Path to terraform.tfvars (default: %(default)s)",
    )
    parser.add_argument("--branch", default="main",
                        help="GitHub branch to deploy (default: %(default)s)")
    parser.add_argument(
        "--phases", nargs="+", default=VALID_PHASES,
        choices=VALID_PHASES, metavar="PHASE",
        help="Phases to run (default: all). Choices: " + " ".join(VALID_PHASES),
    )
    parser.add_argument(
        "--ws-id", default=None, dest="ws_id", metavar="WS_ID",
        help="Existing workspace ID (required when 'create' is not in --phases)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List workspaces matching this repo's name prefix and exit",
    )
    parser.add_argument(
        "--resources", action="store_true",
        help="Print workspace resource list and exit",
    )
    parser.add_argument(
        "--outputs", action="store_true",
        help="Print workspace output variables and exit",
    )
    args = parser.parse_args()

    # ── Early-exit info commands ──────────────────────────────────────────
    if args.list:
        return show_workspace_list(args.tfvars)

    if args.resources or args.outputs:
        ws_id, err = _resolve_ws_id(args.ws_id, args.tfvars, "--resources/--outputs")
        if err:
            print(f"ERROR: {err}")
            return 1
        if args.resources:
            return show_resources(ws_id)
        return show_outputs(ws_id)

    # ── Lifecycle run ─────────────────────────────────────────────────────
    run         = set(args.phases)
    tfvars_path = args.tfvars
    branch      = args.branch

    needs_ws = run & {"plan", "apply", "destroy", "delete"}
    if "create" not in run and needs_ws and not args.ws_id:
        print(
            "ERROR: --ws-id is required when 'create' is not in --phases\n"
            "       Use --list to find the workspace ID."
        )
        return 1

    REPORT_DIR.mkdir(exist_ok=True)
    ts_label    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"lifecycle_{ts_label}.txt"
    log_path    = REPORT_DIR / f"lifecycle_{ts_label}_logs.txt"

    started_at = datetime.now(timezone.utc)
    ws_id      = args.ws_id or None
    ws_name    = None
    phases     = []
    outputs    = {}
    overall    = "FAIL"

    W = 72

    with open(log_path, "w") as lf:

        def section(title):
            bar = "─" * W
            tee(f"\n{bar}\n  {title}\n{bar}", lf)

        def cleanup():
            if not ws_id:
                return
            tee(f"\n  Cleanup: destroying workspace {ws_id} ...", lf)
            run_cmd(f"ibmcloud schematics destroy --id {ws_id} --force", lf=lf, stream=True)
            poll_until_terminal(ws_id, "cleanup-destroy", lf, timeout=JOB_TIMEOUT)
            tee(f"  Cleanup: deleting workspace {ws_id} ...", lf)
            run_cmd(f"ibmcloud schematics workspace delete --id {ws_id} --force", lf=lf)

        def _sigint(sig, frame):
            tee("\n\nInterrupted — running cleanup...", lf)
            cleanup()
            report = render_report(started_at, ws_id, ws_name, phases, outputs, "INTERRUPTED")
            tee(report, lf)
            report_path.write_text(report)
            sys.exit(130)

        signal.signal(signal.SIGINT, _sigint)

        # ── Preflight (always) ────────────────────────────────────────────
        section("PRE-FLIGHT — Check ibmcloud CLI login")
        p = Phase("preflight")
        t0 = time.time()
        try:
            rc, out, err = run_cmd("ibmcloud iam oauth-tokens")
            if rc != 0:
                raise RuntimeError(
                    "Not logged in. Run: ibmcloud login --apikey YOUR_API_KEY -r REGION"
                )
            tee("  ibmcloud CLI authenticated", lf)
            p.status = "PASS"
        except Exception as exc:
            p.status = "FAIL"
            p.error  = str(exc)
            tee(f"  ERROR: {exc}", lf)
        p.duration = int(time.time() - t0)
        phases.append(p)
        if p.status != "PASS":
            report = render_report(started_at, ws_id, ws_name, phases, outputs, "FAIL")
            tee(report, lf); report_path.write_text(report)
            return 1

        # ── Setup (always) ────────────────────────────────────────────────
        section("SETUP — Parse terraform.tfvars → workspace.json")
        p = Phase("setup")
        t0 = time.time()
        try:
            if not Path(tfvars_path).exists():
                raise FileNotFoundError(
                    f"{tfvars_path} not found — "
                    "copy terraform.tfvars.example and fill in your values"
                )
            variables = parse_tfvars(tfvars_path)
            ws        = build_workspace_json(variables, ts_label, branch=branch)
            ws_name   = ws["name"]

            if ws_id:
                try:
                    d = ibmcloud_json(f"ibmcloud schematics workspace get --id {ws_id}", lf)
                    ws_name = d.get("name", ws_id)
                except Exception:
                    ws_name = ws_id

            tee(f"  {len(variables)} variables parsed from {tfvars_path}", lf)
            tee(f"  Workspace name : {ws_name}", lf)
            tee(f"  Branch         : {branch}", lf)
            tee(f"  Location       : {ws['location']}", lf)
            tee(f"  Phases         : {' '.join(ph for ph in VALID_PHASES if ph in run)}", lf)
            if ws_id:
                tee(f"  WS ID (--ws-id): {ws_id}", lf)
            p.status = "PASS"
        except Exception as exc:
            p.status = "FAIL"
            p.error  = str(exc)
            tee(f"  ERROR: {exc}", lf)
        p.duration = int(time.time() - t0)
        phases.append(p)
        if p.status != "PASS":
            report = render_report(started_at, ws_id, ws_name, phases, outputs, "FAIL")
            tee(report, lf); report_path.write_text(report)
            return 1

        # ── Phase: create ─────────────────────────────────────────────────
        if "create" in run:
            section("PHASE — Create workspace")
            p = Phase("create")
            t0 = time.time()
            try:
                rc, out, err = run_cmd(
                    f"ibmcloud schematics workspace new --file {WS_JSON_PATH} --output json"
                )
                if out.strip():
                    print(out, file=lf, flush=True)
                if rc != 0:
                    raise RuntimeError((err or out).strip())
                data  = json.loads(out)
                ws_id = data.get("id") or data.get("workspace_id")
                if not ws_id:
                    raise RuntimeError(f"workspace ID not in response: {out[:300]}")
                tee(f"  Workspace ID : {ws_id}", lf)
                tee("  Waiting for workspace to become ready...", lf)
                status = wait_for_workspace_ready(ws_id, lf)
                tee(f"  Ready status : {status}", lf)
                p.status = "PASS"
            except Exception as exc:
                p.status = "FAIL"
                p.error  = str(exc)
                tee(f"  ERROR: {exc}", lf)
            p.duration = int(time.time() - t0)
            phases.append(p)
            if p.status != "PASS":
                report = render_report(started_at, ws_id, ws_name, phases, outputs, "FAIL")
                tee(report, lf); report_path.write_text(report)
                return 1

        # ── Phase: plan ───────────────────────────────────────────────────
        p_plan = Phase("plan")
        if "plan" in run:
            section("PHASE — Plan workspace")
            t0 = time.time()
            try:
                passed, final_status, elapsed = run_job(
                    cmd              = f"ibmcloud schematics plan --id {ws_id}",
                    ws_id            = ws_id,
                    label            = "plan",
                    lf               = lf,
                    success_statuses = {"INACTIVE", "ACTIVE"},
                    timeout          = JOB_TIMEOUT,
                )
                tee(f"  Final status : {final_status}  ({elapsed}s)", lf)
                p_plan.status = "PASS" if passed else "FAIL"
                if not passed:
                    p_plan.error = f"status after plan: {final_status}"
            except Exception as exc:
                p_plan.status = "FAIL"
                p_plan.error  = str(exc)
                tee(f"  ERROR: {exc}", lf)
            p_plan.duration = int(time.time() - t0)
        phases.append(p_plan)

        # ── Phase: apply ──────────────────────────────────────────────────
        p_apply = Phase("apply")
        if "apply" in run:
            if p_plan.status == "FAIL":
                p_apply.status = "SKIP"
                p_apply.error  = "skipped — plan failed"
            else:
                section("PHASE — Apply workspace")
                t0 = time.time()
                try:
                    passed, final_status, elapsed = run_job(
                        cmd              = f"ibmcloud schematics apply --id {ws_id} --force",
                        ws_id            = ws_id,
                        label            = "apply",
                        lf               = lf,
                        success_statuses = {"ACTIVE"},
                        timeout          = JOB_TIMEOUT,
                    )
                    tee(f"  Final status : {final_status}  ({elapsed}s)", lf)
                    p_apply.status = "PASS" if passed else "FAIL"
                    if not passed:
                        p_apply.error = f"status after apply: {final_status}"
                    if p_apply.status == "PASS":
                        tee("  Fetching outputs...", lf)
                        outputs = fetch_outputs(ws_id, lf)
                except Exception as exc:
                    p_apply.status = "FAIL"
                    p_apply.error  = str(exc)
                    tee(f"  ERROR: {exc}", lf)
                p_apply.duration = int(time.time() - t0)
        phases.append(p_apply)

        # ── Phase: destroy ────────────────────────────────────────────────
        p_destroy = Phase("destroy")
        if "destroy" in run:
            pre = get_ws_status(ws_id) if ws_id else "UNKNOWN"
            if pre in {"INACTIVE", "DRAFT"}:
                p_destroy.status = "SKIP"
                p_destroy.error  = f"no managed state (status={pre})"
            else:
                section("PHASE — Destroy workspace")
                t0 = time.time()
                try:
                    passed, final_status, elapsed = run_job(
                        cmd              = f"ibmcloud schematics destroy --id {ws_id} --force",
                        ws_id            = ws_id,
                        label            = "destroy",
                        lf               = lf,
                        success_statuses = {"INACTIVE"},
                        timeout          = JOB_TIMEOUT,
                    )
                    tee(f"  Final status : {final_status}  ({elapsed}s)", lf)
                    p_destroy.status = "PASS" if passed else "FAIL"
                    if not passed:
                        p_destroy.error = f"status after destroy: {final_status}"
                except Exception as exc:
                    p_destroy.status = "FAIL"
                    p_destroy.error  = str(exc)
                    tee(f"  ERROR: {exc}", lf)
                p_destroy.duration = int(time.time() - t0)
        phases.append(p_destroy)

        # ── Phase: delete ─────────────────────────────────────────────────
        p_delete = Phase("delete")
        if "delete" in run and ws_id:
            section("PHASE — Delete workspace record")
            t0 = time.time()
            try:
                rc, out, err = run_cmd(
                    f"ibmcloud schematics workspace delete --id {ws_id} --force"
                )
                if rc != 0:
                    raise RuntimeError((err or out).strip())
                tee("  Workspace record deleted", lf)
                p_delete.status = "PASS"
            except Exception as exc:
                p_delete.status = "FAIL"
                p_delete.error  = str(exc)
                tee(f"  ERROR: {exc}", lf)
            p_delete.duration = int(time.time() - t0)
        elif "delete" in run:
            p_delete.status = "SKIP"
            p_delete.error  = "no workspace ID — create was skipped"
        phases.append(p_delete)

        # ── Final report ──────────────────────────────────────────────────
        all_run = [p for p in phases if p.status not in {"SKIP"}]
        overall = "PASS" if all(p.status == "PASS" for p in all_run) else "FAIL"

        report = render_report(started_at, ws_id, ws_name, phases, outputs, overall)
        tee(report, lf)
        report_path.write_text(report)

        tee(f"  Log    : {log_path}", lf)
        tee(f"  Report : {report_path}", lf)

        return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
