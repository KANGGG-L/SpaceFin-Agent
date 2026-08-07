"""CLI 测试：各子命令可用（离线为主，避免网络）。"""

import csv
import subprocess
import sys

import pytest

FIXTURE = "anjuke_crawler/tests/sample_listing.html"


def run_cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "anjuke_crawler.main", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


@pytest.fixture
def tools_dir():
    import os

    # tests/ -> anjuke_crawler/ -> tools/
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_cli_help_lists_subcommands(tools_dir):
    r = run_cli("--help", cwd=tools_dir)
    assert r.returncode == 0
    for sub in ["parse", "parse-advanced", "geocode", "crawl", "stealth"]:
        assert sub in r.stdout


def test_cli_parse(tools_dir, tmp_path):
    out = tmp_path / "p.csv"
    r = run_cli(
        "parse", "--html", FIXTURE, "--district", "sh_pudong", "--out", str(out), cwd=tools_dir
    )
    assert r.returncode == 0
    assert len(list(csv.DictReader(open(out, encoding="utf-8-sig")))) == 71


def test_cli_parse_advanced(tools_dir, tmp_path):
    out = tmp_path / "a.csv"
    r = run_cli("parse-advanced", "--html", FIXTURE, "--out", str(out), cwd=tools_dir)
    assert r.returncode == 0
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert len(rows) == 71
    assert "Layout" in rows[0]


def test_cli_geocode(tools_dir):
    r = run_cli("geocode", "--name", "证大家园", cwd=tools_dir)
    assert r.returncode == 0
    assert "lat=31" in r.stdout
