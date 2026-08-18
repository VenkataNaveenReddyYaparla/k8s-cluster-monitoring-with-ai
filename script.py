#!/usr/bin/env python3
import argparse
import datetime
import html
import os
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ---------------------------------------------------------------------------
# .env loading (no extra dependency)
# ---------------------------------------------------------------------------

def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def env_optional(name: str, default=None):
    value = os.environ.get(name)
    return value if value else default


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got: {value}")


def env_list(name: str, required: bool = True):
    value = os.environ.get(name)
    if not value:
        if required:
            raise SystemExit(f"Missing required environment variable: {name}")
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


load_env_file()

PROMETHEUS_URL = env_required("PROMETHEUS_URL").rstrip("/")
# Optional auth for Prometheus instances that sit behind a reverse proxy or
# have auth enabled -- a bearer token takes precedence over basic auth if
# both are somehow set.
PROMETHEUS_BEARER_TOKEN = env_optional("PROMETHEUS_BEARER_TOKEN")
PROMETHEUS_USER = env_optional("PROMETHEUS_USER")
PROMETHEUS_PASS = env_optional("PROMETHEUS_PASS")
if bool(PROMETHEUS_USER) != bool(PROMETHEUS_PASS):
    raise SystemExit("PROMETHEUS_USER and PROMETHEUS_PASS must both be set, or both left unset.")

GEMINI_API_KEY = env_required("GEMINI_API_KEY")
GEMINI_MODEL = env_optional("GEMINI_MODEL", "auto")
MAX_ROWS_PER_QUERY = env_int("MAX_ROWS_PER_QUERY", 30)

# SMTP: SMTP_SERVER may be one host or a comma-separated "host[:port]" list;
# each is tried in order until one send succeeds. SMTP_USER/SMTP_PASS are
# optional -- set both to authenticate, or leave both blank for an open
# relay with no password. SMTP_SECURITY is "none" | "starttls" | "ssl".
SMTP_SERVER_LIST = env_list("SMTP_SERVER")
SMTP_PORT_DEFAULT = env_int("SMTP_PORT", 25)
SMTP_SECURITY = (env_optional("SMTP_SECURITY", "none") or "none").lower()
if SMTP_SECURITY not in ("none", "starttls", "ssl"):
    raise SystemExit("SMTP_SECURITY must be one of: none, starttls, ssl")
SMTP_USER = env_optional("SMTP_USER")
SMTP_PASS = env_optional("SMTP_PASS")
if bool(SMTP_USER) != bool(SMTP_PASS):
    raise SystemExit("SMTP_USER and SMTP_PASS must both be set, or both left unset.")

EMAIL_USER = env_required("EMAIL_USER")
EMAIL_TO = env_list("EMAIL_TO")


def _parse_smtp_targets():
    """Turn SMTP_SERVER_LIST entries ("host" or "host:port") into (host, port) tuples."""
    targets = []
    for entry in SMTP_SERVER_LIST:
        host = entry
        port = SMTP_PORT_DEFAULT
        if ":" in entry:
            host, port_str = entry.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                raise SystemExit(f"Invalid port in SMTP_SERVER entry: {entry}")
        targets.append((host, port))
    return targets


SMTP_TARGETS = _parse_smtp_targets()


# ---------------------------------------------------------------------------
# retry helper
# ---------------------------------------------------------------------------

def retry(fn, attempts=3, backoff_seconds=0.5, retryable=(Exception,)):
    """Call fn() up to `attempts` times with linear backoff. Re-raises the
    last exception if every attempt fails."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except retryable as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(backoff_seconds * attempt)
    raise last_exc


# ---------------------------------------------------------------------------
# Step 1: Query Prometheus
# ---------------------------------------------------------------------------

# PromQL queries. Queries that depend on kube-state-metrics, kubelet/cAdvisor,
# Alertmanager, cert-manager, etcd, or Longhorn will return "No data returned"
# when those metric families are not exposed by this Prometheus instance.
QUERIES = {
    "Firing Alerts": 'ALERTS{alertstate="firing"}',
    "Node Not Ready": 'kube_node_status_condition{condition="Ready",status!="true"}',
    "Node Pressure Conditions": (
        'kube_node_status_condition{condition=~"MemoryPressure|DiskPressure|PIDPressure|NetworkUnavailable",status="true"}'
    ),
    "Unschedulable Nodes": "kube_node_spec_unschedulable == 1",
    "CPU Usage (%)": (
        "100 - (avg by (node) " '(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
    ),
    "Memory Usage (%)": (
        "100 * (1 - (avg by (node) (node_memory_MemAvailable_bytes) "
        "/ avg by (node) (node_memory_MemTotal_bytes)))"
    ),
    "Top 10 Pods by CPU Usage (cores)": (
        "topk(10, sum by (namespace, pod) "
        '(rate(container_cpu_usage_seconds_total{container!="",pod!=""}[5m])))'
    ),
    "Top 10 Pods by Memory Usage (bytes)": (
        "topk(10, sum by (namespace, pod) "
        '(container_memory_working_set_bytes{container!="",pod!=""}))'
    ),
    "Disk Usage (%) - root filesystem": (
        '100 - ((node_filesystem_avail_bytes{fstype!~"tmpfs|overlay",mountpoint="/"} * 100) '
        '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay",mountpoint="/"})'
    ),
    "Disk Usage (%) - all mounted filesystems": (
        '100 - ((node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} * 100) '
        '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay"})'
    ),
    "PVC Usage (%)": "100 * kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes",
    "PVCs Not Bound": 'kube_persistentvolumeclaim_status_phase{phase!="Bound"} == 1',
    # osdk-hourly-import is a known/expected noisy job -- excluded (pod!~"osdk-hourly-import.*")
    # from pod-health queries so it doesn't clutter the report with expected failures.
    "Pods Pending Failed or Unknown": (
        'kube_pod_status_phase{phase=~"Pending|Failed|Unknown",pod!~"osdk-hourly-import.*"} == 1'
    ),
    "Pods Not Ready": 'kube_pod_status_ready{condition="false",pod!~"osdk-hourly-import.*"} == 1',
    "Container Waiting Reasons": (
        'kube_pod_container_status_waiting_reason{reason=~"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|RunContainerError",pod!~"osdk-hourly-import.*"} == 1'
    ),
    "Container Restarts in Last Hour": (
        'increase(kube_pod_container_status_restarts_total{pod!~"osdk-hourly-import.*"}[1h]) > 0'
    ),
    "Network Packets Received (pkts/s)": 'rate(node_network_receive_packets_total{device!="lo"}[5m])',
    "Network Packets Transmitted (pkts/s)": 'rate(node_network_transmit_packets_total{device!="lo"}[5m])',
    "Network Receive Errors (errors/s)": 'rate(node_network_receive_errs_total{device!="lo"}[5m])',
    "Network Transmit Errors (errors/s)": 'rate(node_network_transmit_errs_total{device!="lo"}[5m])',
    "Kubernetes API Server Up": 'up{job=~"apiserver|kube-apiserver"}',
    "Etcd Has Leader": "etcd_server_has_leader",
    "Certificates Expiring Within 30 Days": (
        "(certmanager_certificate_expiration_timestamp_seconds - time()) < 2592000"
    ),
    "Longhorn Volume Robustness": "longhorn_volume_robustness",
    "Longhorn Volume Actual Size (bytes)": "longhorn_volume_actual_size_bytes",
}


def query_prometheus(promql: str):
    """Run an instant query against Prometheus and return the result list.
    Retries transient network errors a few times before giving up."""
    url = f"{PROMETHEUS_URL}/api/v1/query"

    headers = {}
    auth = None
    if PROMETHEUS_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {PROMETHEUS_BEARER_TOKEN}"
    elif PROMETHEUS_USER and PROMETHEUS_PASS:
        auth = (PROMETHEUS_USER, PROMETHEUS_PASS)

    def do_request():
        resp = requests.get(url, params={"query": promql}, headers=headers, auth=auth, timeout=15)
        resp.raise_for_status()
        return resp

    try:
        resp = retry(do_request, attempts=3, backoff_seconds=0.5, retryable=(requests.RequestException,))
    except requests.RequestException as e:
        return {"error": str(e)}

    try:
        data = resp.json()
        if data.get("status") != "success":
            return {"error": data.get("error", "unknown error")}
        return data["data"]["result"]
    except ValueError as e:
        return {"error": f"invalid JSON response: {e}"}


def collect_metrics():
    """Run all configured queries and collect results into a text blob."""
    lines = []
    for label, promql in QUERIES.items():
        result = query_prometheus(promql)
        lines.append(f"\n### {label}")
        if isinstance(result, dict) and "error" in result:
            lines.append(f"  ERROR: {result['error']}")
            continue
        if not result:
            lines.append("  No data returned.")
            continue
        for series in result[:MAX_ROWS_PER_QUERY]:  # cap per-metric rows to keep prompt small
            metric_labels = series.get("metric", {})
            value = series.get("value", [None, None])[1]
            label_str = ", ".join(f"{k}={v}" for k, v in metric_labels.items() if k != "__name__")
            lines.append(f"  {label_str}: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 2: Pass to Google Gemini for summary
# ---------------------------------------------------------------------------

# Preferred model name fragments, best first. `models.list()` can return
# names that are still listed but actually rejected at request time, so we
# try candidates in this order and fall through on failure.
MODEL_PREFERENCE = [
    "flash-latest",
    "3.6-flash",
    "3.5-flash",
    "3.1-flash-lite",
    "3-flash",
    "2.5-flash-lite",
    "2.5-flash",
]

PROMPT_TEMPLATE = """You are a Kubernetes/infrastructure monitoring assistant.
Below are raw Prometheus metric readings for a Kubernetes cluster. Write a
concise operations email summary that highlights only meaningful findings.

Cover these areas:
- Overall health status as Normal, Warning, or Critical, with a short reason
- Firing Prometheus alerts
- NotReady, unschedulable, or pressure-affected nodes
- Nodes over 80% CPU or memory
- Filesystems or PVCs over 80% usage, PVCs not Bound, and Longhorn volume issues
- Pods that are Pending, Failed, Unknown, Not Ready, CrashLoopBackOff, ImagePullBackOff, or restarting
- Top CPU and memory consuming pods when they stand out
- Nonzero network receive/transmit errors or unusually high packet rates
- API server, etcd, or certificate-expiry concerns
- Clear next actions for anything that needs attention

If a metric section says "No data returned", do not treat that as a failure.
Mention missing monitoring coverage only if an important area has no data and
that absence limits confidence in the health assessment.

Formatting rules (strict):
- Output PLAIN TEXT only. This will be pasted directly into an email body.
- Do NOT use Markdown. No asterisks (**bold**), no underscores, no backticks,
  no headers (#).
- Use exactly these section labels, each on its own line, in this order:
  "Overall:", "Critical:", "Warnings:", "Top usage:", "Actions:".
- Put the health status and reason on the line right after "Overall:"
  (e.g. "Warning - reason here"), not appended to the label itself.
- Leave one blank line after every section label, before its findings.
- Leave one blank line between sections.
- Use a simple dash "-" at the start of each finding line, one finding per
  line, with a blank line between findings.
- If a section has nothing to report, write "- None." under that label
  instead of skipping the section, so the structure stays consistent.
- Node/interface/mount names should be written plain, e.g. lnx-bss-compcluster3,
  not wrapped in backticks or asterisks.
- Keep the full summary under 30 lines.

Raw metrics:
{metrics_text}
"""

# Section labels the model is instructed to use, in order. Used by
# strip_markdown() to enforce blank-line separation even if the model
# doesn't follow spacing instructions perfectly.
SECTION_LABELS = ("Overall:", "Critical:", "Warnings:", "Top usage:", "Actions:")


def strip_markdown(text: str) -> str:
    """Remove common Markdown artifacts so the email body never shows literal
    ** or ` characters, and enforce consistent blank-line spacing between
    section labels and findings -- even if the model doesn't follow the
    prompt's spacing instructions exactly."""
    if not text:
        return text
    text = text.replace("**", "").replace("__", "").replace("`", "")
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip("#").lstrip()
        lines.append(stripped if line.lstrip().startswith("#") else line)
    text = "\n".join(lines)

    # Drop any blank lines the model already added -- we re-add our own so
    # spacing is consistent regardless of what the model produced.
    raw_lines = [ln for ln in text.split("\n") if ln.strip() != ""]
    spaced = []
    for i, ln in enumerate(raw_lines):
        is_section_label = any(ln.strip().startswith(label) for label in SECTION_LABELS)
        is_bullet = ln.lstrip().startswith("- ")
        if i > 0 and (is_section_label or is_bullet) and spaced and spaced[-1] != "":
            spaced.append("")
        spaced.append(ln)
    return "\n".join(spaced)


def ranked_candidates(client):
    """Return this key's generateContent-capable models, ranked by preference."""
    candidates = []
    try:
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" in actions:
                candidates.append(m.name)
    except Exception as e:
        return [], f"Could not list models: {e}"

    if not candidates:
        return [], "No models with generateContent support were returned for this key."

    def rank(name):
        low = name.lower()
        for i, frag in enumerate(MODEL_PREFERENCE):
            if frag in low:
                return i
        return len(MODEL_PREFERENCE)

    return sorted(candidates, key=rank), candidates


def get_ai_summary(metrics_text: str) -> str:
    if not GEMINI_API_KEY or GEMINI_API_KEY.lower() == "put_your_gemini_api_key_here":
        return "ERROR: set GEMINI_API_KEY in your .env file."

    try:
        from google import genai
    except ImportError:
        return "ERROR: install the SDK first -> pip install google-genai"

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = PROMPT_TEMPLATE.format(metrics_text=metrics_text)

    # Show exactly what is being sent to Gemini.
    print("\n===== PROMPT SENT TO GEMINI =====")
    print(prompt)

    if GEMINI_MODEL and GEMINI_MODEL != "auto":
        try_order = [GEMINI_MODEL]
    else:
        try_order, all_models = ranked_candidates(client)
        if not try_order:
            return f"ERROR: {all_models}"
        print(f"(will try models in this order: {try_order})")

    last_error = None
    for model_name in try_order:
        try:
            response = retry(
                lambda: client.models.generate_content(model=model_name, contents=prompt),
                attempts=2,
                backoff_seconds=0.5,
            )
            print(f"(succeeded with model: {model_name})")
            return strip_markdown(response.text)
        except Exception as e:
            print(f"(model '{model_name}' failed: {e})")
            last_error = e
            continue

    return f"ERROR: all candidate models failed. Last error: {last_error}"


# ---------------------------------------------------------------------------
# Step 3: Email the summary
# ---------------------------------------------------------------------------

# Labels bolded in the HTML email. Deliberately excludes "Overall:" -- only
# these four were asked to be bold.
BOLD_LABELS = ("Critical:", "Warnings:", "Top usage:", "Actions:")


def _text_to_html(body: str) -> str:
    """Convert the plain-text summary to simple HTML, bolding Critical:,
    Warnings:, Top usage:, and Actions: so they stand out in mail clients
    that render HTML. Plain text has no concept of bold, so this is the
    only way to actually make those labels bold in the email."""
    html_lines = []
    for line in body.split("\n"):
        escaped = html.escape(line) if line else "&nbsp;"
        if any(line.strip().startswith(label) for label in BOLD_LABELS):
            html_lines.append(f"<b>{escaped}</b>")
        else:
            html_lines.append(escaped)
    joined = "<br>\n".join(html_lines)
    return (
        '<html><body style="font-family: Arial, Helvetica, sans-serif; '
        f'font-size: 14px;">{joined}</body></html>'
    )


def _build_message(subject: str, body: str) -> MIMEMultipart:
    # Force plain 7-bit ASCII instead of utf-8, since utf-8 triggers
    # quoted-printable/base64 encoding that some relays mangle.
    safe_body = body.encode("ascii", errors="replace").decode("ascii")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = ", ".join(EMAIL_TO)

    # Attach plain text first, HTML second -- mail clients prefer the last
    # part they can render, so HTML-capable clients show bold section
    # labels while plain-text-only clients/relays fall back cleanly.
    msg.attach(MIMEText(safe_body, "plain", "us-ascii"))
    msg.attach(MIMEText(_text_to_html(safe_body), "html", "us-ascii"))
    return msg


def send_email(subject: str, body: str):
    """Try each configured SMTP target in order until one send succeeds."""
    if not SMTP_TARGETS:
        print("ERROR sending email: no SMTP_SERVER configured.")
        return

    message = _build_message(subject, body)
    last_error = None

    for host, port in SMTP_TARGETS:
        try:
            smtp_cls = smtplib.SMTP_SSL if SMTP_SECURITY == "ssl" else smtplib.SMTP
            with smtp_cls(host, port, timeout=20) as server:
                if SMTP_SECURITY == "starttls":
                    server.starttls()
                if SMTP_USER and SMTP_PASS:
                    server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(EMAIL_USER, EMAIL_TO, message.as_string())
            print(f"Email sent via {host}:{port} to {EMAIL_TO}")
            return
        except Exception as e:
            print(f"SMTP send via {host}:{port} failed: {e}")
            last_error = e
            continue

    print(f"ERROR sending email: all SMTP targets failed. Last error: {last_error}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Query Prometheus, summarize with Gemini, email the report.")
    parser.add_argument("--dry-run", action="store_true", help="Collect + summarize, but do not send email.")
    parser.add_argument("--no-ai", action="store_true", help="Skip Gemini and email the raw metrics instead.")
    args = parser.parse_args()

    print(f"Querying Prometheus at {PROMETHEUS_URL} ...")
    metrics_text = collect_metrics()

    print("\n===== RAW METRICS =====")
    print(metrics_text)

    if args.no_ai:
        summary = metrics_text
    else:
        print("\nSending metrics to Gemini for summary...")
        summary = get_ai_summary(metrics_text)

    print("\n===== SUMMARY =====")
    print(summary)

    # Never email a bare "ERROR: ..." string as if it were the health report.
    if not args.no_ai and summary.startswith("ERROR"):
        summary = f"AI summary unavailable: {summary}\n\nRaw Prometheus metrics follow:\n{metrics_text}"

    if args.dry_run:
        print("\nDry run: skipping email send.")
        return

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"Cluster Health Summary - {timestamp}"

    print("\nSending email...")
    send_email(subject, summary)


if __name__ == "__main__":
    main()
