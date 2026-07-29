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
    # CORES, not a percentage. The metric is a rate of cpu-seconds per second,
    # which IS a core count: 0.25 = a quarter of one core. Grafana has no
    # built-in cores unit, so use a custom suffix — "percentunit" would render
    # 0.25 as "25%", which reads as a proportion of something it is not.
    "suffix: cores", 2, {"h": 9, "w": 16, "x": 0, "y": 1},
    desc="CPU cores consumed, summed across all replicas of the workload. "
         "1 core = 1 full CPU. A t3.medium node has 2 cores, so 0.5 here is a "
         "quarter of one node. Not a percentage — see 'CPU vs requests' for that.",
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
    desc="Cores used as a percentage of the cores REQUESTED. 100% means the "
         "workload is using exactly what the scheduler reserved for it. Above "
         "100% is not an error — CPU is compressible, so it is borrowing idle "
         "capacity it has no guarantee of keeping. Sustained above 100% means "
         "the request is set too low.",
))

panels.append(timeseries(
    "CPU throttling by service",
    join(f'rate(container_cpu_cfs_throttled_seconds_total{{{CONTAINER_SEL}}}[$__rate_interval])'),
    # Genuinely a ratio here: seconds throttled per second elapsed, 0-1.
    "percentunit", 4, {"h": 8, "w": 24, "x": 0, "y": 10},
    desc="Percentage of time the workload spent throttled against its CPU "
         "LIMIT — 20% means one second in five was spent waiting for CPU it was "
         "not allowed to use. Anything sustained above zero means the limit is "
         "actively slowing the service.\n\n"
         "EXPECTED TO BE EMPTY on this cluster: the underlying metric only "
         "exists for containers that set a CPU limit, and these workloads set "
         "requests only. No data here means 'no limits configured', not "
         "'no throttling'.",
))

# --- Memory ----------------------------------------------------------------
panels.append(row("Memory", 5, 18))

panels.append(timeseries(
    "Memory usage by service",
    join(f'container_memory_working_set_bytes{{{CONTAINER_SEL}}}'),
    "bytes", 6, {"h": 9, "w": 16, "x": 0, "y": 19},
    desc="Memory in use, summed across all replicas of the workload.\n\n"
         "This is the WORKING SET: memory the kernel cannot reclaim under "
         "pressure. It is deliberately not container_memory_usage_bytes, which "
         "includes reclaimable page cache and so overstates real usage. The "
         "working set is the number the OOM killer compares against the limit.",
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
    desc="Memory in use as a percentage of the memory LIMIT.\n\n"
         "Unlike CPU, this is a hard ceiling — memory is not compressible, so "
         "reaching 100% is an OOMKill and a restart, not a slowdown. Treat "
         "sustained red as an imminent restart. Blank rows are workloads with "
         "no memory limit set, which cannot be OOMKilled by their own limit but "
         "can still be evicted under node pressure.",
    thresholds=(75, 90),
))

panels.append(timeseries(
    "Memory headroom before OOMKill",
    # limit - usage, so the y-axis is "how much room is left" and a series
    # trending toward zero is a restart about to happen. Workloads with no
    # limit drop out of the join entirely rather than showing a false infinity.
    join(f'kube_pod_container_resource_limits{{namespace=~"$namespace", resource="memory"}}')
    + "\n-\n"
    + join(f'container_memory_working_set_bytes{{{CONTAINER_SEL}}}'),
    "bytes", 8, {"h": 8, "w": 12, "x": 0, "y": 28},
    desc="Bytes remaining before the memory limit is hit, per workload.\n\n"
         "A series trending toward zero is heading for an OOMKill — this is the "
         "same information as 'Memory vs limits' but as an absolute amount, "
         "which is easier to reason about when sizing a limit.\n\n"
         "Workloads with no memory limit set are absent from this panel by "
         "design: they have no ceiling to run out of.",
))

panels.append(timeseries(
    "Container restarts by service",
    join(f'increase(kube_pod_container_status_restarts_total{{namespace=~"$namespace"}}[$__rate_interval])'),
    "suffix: restarts", 9, {"h": 8, "w": 12, "x": 12, "y": 28},
    desc="Container restarts, counted per graph interval rather than as a "
         "running total, so each spike is one restart event.\n\n"
         "Line up a spike here with 'Memory headroom' to the left: a restart "
         "immediately after headroom reached zero is an OOMKill. A restart with "
         "headroom to spare is a crash or a failed probe instead.",
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
