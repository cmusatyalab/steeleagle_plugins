"""The plugin's args, and the Prometheus config they stand for.

Prometheus scrapes two jobs: every daemon's Uriel metrics, whose addresses it
asks the GCS for (HTTP service discovery), and the GCS's own metrics.
"""
import dataclasses
import json
from urllib.parse import urlsplit

DEFAULT_LISTEN_ADDRESS = ":9090"
DEFAULT_SCRAPE_INTERVAL = "5s"
DEFAULT_RETENTION = "1d"
DEFAULT_GCS_URL = "http://localhost:8002"
DEFAULT_STORAGE_DIR = "data"

# How often Prometheus asks the GCS which daemons to scrape.
SD_REFRESH_INTERVAL = "30s"
TARGETS_PATH = "/api/metrics/targets"
METRICS_PATH = "/metrics"
READY_PATH = "/-/ready"
DAEMONS_JOB = "eagled"
GCS_JOB = "gcs"
# Where Prometheus is reached when listen_address names only a port.
LOCAL_HOST = "localhost"


@dataclasses.dataclass(frozen=True)
class Args:
    listen_address: str = DEFAULT_LISTEN_ADDRESS
    scrape_interval: str = DEFAULT_SCRAPE_INTERVAL
    retention: str = DEFAULT_RETENTION
    gcs_url: str = DEFAULT_GCS_URL
    storage_dir: str = DEFAULT_STORAGE_DIR


def parse_args(raw):
    """The args in the JSON object `raw` (STEELEAGLE_PLUGIN_ARGS), with unset
    ones defaulted. Raises ValueError for an arg the plugin doesn't have."""
    given = json.loads(raw) if raw else {}
    known = {field.name for field in dataclasses.fields(Args)}
    unknown = set(given) - known
    if unknown:
        raise ValueError(f"unknown args: {', '.join(sorted(unknown))}")
    return Args(**{name: value for name, value in given.items() if value is not None})


def render_config(args):
    """Prometheus's config, as a dict to dump as YAML."""
    gcs_url = args.gcs_url.rstrip("/")
    return {
        "global": {"scrape_interval": args.scrape_interval},
        "scrape_configs": [
            {
                "job_name": DAEMONS_JOB,
                "http_sd_configs": [
                    {"url": gcs_url + TARGETS_PATH, "refresh_interval": SD_REFRESH_INTERVAL}
                ],
            },
            {
                "job_name": GCS_JOB,
                "metrics_path": METRICS_PATH,
                "static_configs": [{"targets": [urlsplit(gcs_url).netloc]}],
            },
        ],
    }


def ready_url(args):
    """Where Prometheus answers once it is ready to serve queries."""
    host, _, port = args.listen_address.rpartition(":")
    return f"http://{host or LOCAL_HOST}:{port}{READY_PATH}"
