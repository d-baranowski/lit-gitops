#!/usr/bin/env python3
"""Generates workload-resources.json — per-SERVICE CPU and memory.

Hand-written rather than vendored from grafana.com, because every community
dashboard in this space is per-POD: each replica gets its own line, and pod
names churn on every rollout, so the graph is unreadable and nothing is
comparable across a deploy. This one aggregates replicas of the same workload
into a single series.

Run after editing, then commit the JSON (the JSON is what Flux ships):

    python3 build-workload-resources.py

THE GROUPING TRICK
------------------
cAdvisor metrics (container_*) carry `pod` but no notion of which Deployment a
pod belongs to. kube_pod_owner supplies that, so every query joins through it:

    owner_kind=ReplicaSet  -> owner_name is "<deployment>-<rshash>", strip the hash
    anything else          -> owner_name is already the workload (StatefulSet,
                              DaemonSet, Job)

Stripping the pod name directly with chained label_replace does NOT work: the
second regex re-matches the still-unmodified `pod` label and clobbers the first
result, turning utr-staging-core into utr-staging-core-96f99c6bd. The join is
the reliable form.
"""

import json

DS = {"type": "prometheus", "uid": "${datasource}"}

# Resolves pod -> workload. Reused by every query in this dashboard.
WORKLOAD = (
    'label_replace(kube_pod_owner{namespace=~"$namespace", owner_kind="ReplicaSet"},'
    ' "workload", "$1", "owner_name", "^(.+)-[^-]+$")'
    " or "
    'label_replace(kube_pod_owner{namespace=~"$namespace", owner_kind!="ReplicaSet"},'
    ' "workload", "$1", "owner_name", "^(.+)$")'
)

# container!="" drops the pod-level cgroup rollup; without it every value is
# double-counted. container!="POD" drops the pause container.
CONTAINER_SEL = 'namespace=~"$namespace", container!="", container!="POD", pod=~".+"'


def join(inner):
    """Sum an instant/range vector by workload, joining through kube_pod_owner."""
    return (
        f"sum by (workload) (\n  {inner}\n"
        f"  * on (namespace, pod) group_left(workload) (\n    {WORKLOAD}\n  )\n)"
    )


def target(expr, legend="{{workload}}"):
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": "A",
        "editorMode": "code",
        "range": True,
    }


def timeseries(title, expr, unit, pid, gp, desc="", legend="{{workload}}"):
    return {
        "type": "timeseries",
        "title": title,
        "description": desc,
        "id": pid,
        "gridPos": gp,
        "datasource": DS,
        "targets": [target(expr, legend)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": 0,
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "fillOpacity": 8,
                    "showPoints": "never",
                    "spanNulls": True,
                    "stacking": {"mode": "none"},
                },
                "color": {"mode": "palette-classic"},
            },
            "overrides": [],
        },
        "options": {
            # Table legend with numbers: at a glance you want "which service is
            # heaviest", which a plain list legend cannot answer.
            "legend": {
                "displayMode": "table",
                "placement": "right",
                "showLegend": True,
                "calcs": ["mean", "max", "lastNotNull"],
                "sortBy": "Max",
                "sortDesc": True,
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def gauge_table(title, expr, pid, gp, desc="", thresholds=(70, 90)):
    """Percentage-of-allocation table with a colour bar."""
    return {
        "type": "table",
        "title": title,
        "description": desc,
        "id": pid,
        "gridPos": gp,
        "datasource": DS,
        "targets": [dict(target(expr), instant=True, range=False, format="table")],
        "transformations": [
            {"id": "organize", "options": {
                "excludeByName": {"Time": True},
                "renameByName": {"workload": "Service", "Value": "% of allocation"},
            }},
            {"id": "sortBy", "options": {"fields": {}, "sort": [{"field": "% of allocation", "desc": True}]}},
        ],
        "fieldConfig": {
            "defaults": {
                "unit": "percent",
                "custom": {"align": "auto", "cellOptions": {"type": "gauge", "mode": "gradient"}},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "green", "value": None},
                        {"color": "orange", "value": thresholds[0]},
                        {"color": "red", "value": thresholds[1]},
                    ],
                },
                "max": 150,
                "min": 0,
            },
            "overrides": [],
        },
        "options": {"showHeader": True},
    }


def row(title, pid, y):
    return {"type": "row", "title": title, "id": pid, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "collapsed": False, "panels": []}


panels = []

# --- CPU -------------------------------------------------------------------
panels.append(row("CPU", 1, 0))

panels.append(timeseries(
    "CPU usage by service",
    join(f'rate(container_cpu_usage_seconds_total{{{CONTAINER_SEL}}}[$__rate_interval])'),
    "percentunit", 2, {"h": 9, "w": 16, "x": 0, "y": 1},
    desc="Cores consumed, summed across all replicas of each workload. "
         "1.0 = one full core.",
))

panels.append(gauge_table(
    "CPU vs requests",
    # Requests are what the scheduler reserved. Sustained >100% means the
    # workload is borrowing idle capacity it is not guaranteed to keep.
    "100 * " + join(f'rate(container_cpu_usage_seconds_total{{{CONTAINER_SEL}}}[$__rate_interval])')
    # Both sides are already `sum by (workload)`, so their label sets are
    # identical and a plain `/` matches one-to-one. An ignoring()/group_left
    # here would strip the only shared label and fail as a many-to-many.
    + "\n/\n"
    + join(f'kube_pod_container_resource_requests{{namespace=~"$namespace", resource="cpu"}}'),
    3, {"h": 9, "w": 8, "x": 16, "y": 1},
    desc="Usage as a percentage of the CPU request. Over 100% is not an error — "
         "it means the workload is using burst capacity that is not reserved for it.",
))

panels.append(timeseries(
    "CPU throttling by service",
    join(f'rate(container_cpu_cfs_throttled_seconds_total{{{CONTAINER_SEL}}}[$__rate_interval])'),
    "percentunit", 4, {"h": 8, "w": 24, "x": 0, "y": 10},
    desc="Seconds per second spent throttled against the CPU limit. Anything "
         "sustained above zero means the limit is actively slowing the service. "
         "Empty is normal here: this metric only exists for containers that set "
         "a CPU limit, and most workloads in this cluster set requests only.",
))

# --- Memory ----------------------------------------------------------------
panels.append(row("Memory", 5, 18))

panels.append(timeseries(
    "Memory usage by service",
    join(f'container_memory_working_set_bytes{{{CONTAINER_SEL}}}'),
    "bytes", 6, {"h": 9, "w": 16, "x": 0, "y": 19},
    desc="Working set — what the kernel cannot reclaim under pressure. This is "
         "the number the OOM killer acts on, not container_memory_usage_bytes.",
))

panels.append(gauge_table(
    "Memory vs limits",
    # Against LIMITS, not requests: for memory, crossing the limit is fatal
    # (OOMKill) rather than merely slow, so this is the number that predicts a
    # restart.
    "100 * " + join(f'container_memory_working_set_bytes{{{CONTAINER_SEL}}}')
    + "\n/\n"
    + join(f'kube_pod_container_resource_limits{{namespace=~"$namespace", resource="memory"}}'),
    7, {"h": 9, "w": 8, "x": 16, "y": 19},
    desc="Working set as a percentage of the memory limit. Unlike CPU this is a "
         "hard ceiling: reaching 100% is an OOMKill, so treat sustained red as "
         "an imminent restart.",
    thresholds=(75, 90),
))

panels.append(timeseries(
    "Memory usage vs requests and limits",
    join(f'container_memory_working_set_bytes{{{CONTAINER_SEL}}}'),
    "bytes", 8, {"h": 8, "w": 12, "x": 0, "y": 28},
    desc="Same series as above, kept separate so it can be compared against the "
         "allocation lines without the legend table crowding the plot.",
))

panels.append(timeseries(
    "Container restarts by service",
    join(f'increase(kube_pod_container_status_restarts_total{{namespace=~"$namespace"}}[$__rate_interval])'),
    "short", 9, {"h": 8, "w": 12, "x": 12, "y": 28},
    desc="Restarts per interval. Correlate a step here with the memory panel "
         "above — a restart following a climb to the limit is an OOMKill.",
))

dashboard = {
    "uid": "utro-workload-resources",
    "title": "Utro / Workload Resources",
    "description": (
        "Per-service CPU and memory. Replicas of the same Deployment/StatefulSet/"
        "DaemonSet are aggregated into one series, so the view survives a rollout "
        "and is comparable over time."
    ),
    "tags": ["utro", "resources", "generated"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-3h", "to": "now"},
    "editable": True,
    "graphTooltip": 1,  # shared crosshair across panels
    "panels": panels,
    "templating": {
        "list": [
            {
                "name": "datasource",
                "type": "datasource",
                "query": "prometheus",
                "label": "Data source",
                # Pinned: leaving this empty is what makes vendored dashboards
                # show "No data" on every panel.
                "current": {"selected": False, "text": "prometheus", "value": "prometheus"},
                "hide": 0,
            },
            {
                "name": "namespace",
                "type": "query",
                "label": "Namespace",
                "datasource": DS,
                "query": {"query": "label_values(kube_pod_owner, namespace)", "refId": "ns"},
                "refresh": 2,
                "multi": True,
                "includeAll": True,
                "current": {"selected": True, "text": ["default"], "value": ["default"]},
                "sort": 1,
                "hide": 0,
            },
        ]
    },
}

with open("workload-resources.json", "w") as fh:
    json.dump(dashboard, fh, indent=2)
    fh.write("\n")

print(f"wrote workload-resources.json ({len(panels)} panels)")
