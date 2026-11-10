import pytest

from launcher.config import Args, parse_args, ready_url, render_config


def test_defaults():
    assert parse_args(None) == Args(
        listen_address=":9090",
        scrape_interval="5s",
        retention="1d",
        gcs_url="http://localhost:8002",
        storage_dir="data",
    )


def test_set_args_replace_defaults():
    assert parse_args('{"retention": "2d"}').retention == "2d"


def test_unknown_arg_is_rejected():
    with pytest.raises(ValueError):
        parse_args('{"retentoin": "2d"}')


def test_config_discovers_daemons_from_the_gcs():
    config = render_config(parse_args('{"gcs_url": "http://lambda2:8002"}'))

    assert config["global"]["scrape_interval"] == "5s"
    [daemons] = [job for job in config["scrape_configs"] if job["job_name"] == "eagled"]
    assert daemons["http_sd_configs"] == [
        {"url": "http://lambda2:8002/api/metrics/targets", "refresh_interval": "30s"}
    ]


def test_config_scrapes_the_gcs_itself():
    config = render_config(parse_args('{"gcs_url": "http://lambda2:8002"}'))

    [gcs] = [job for job in config["scrape_configs"] if job["job_name"] == "gcs"]
    assert gcs["static_configs"] == [{"targets": ["lambda2:8002"]}]
    assert gcs["metrics_path"] == "/metrics"


def test_ready_url_uses_localhost_for_a_bare_port():
    assert ready_url(parse_args(None)) == "http://localhost:9090/-/ready"


def test_ready_url_keeps_a_given_host():
    assert ready_url(parse_args('{"listen_address": "127.0.0.1:9091"}')) == "http://127.0.0.1:9091/-/ready"
