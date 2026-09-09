import csv
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from soramimic_video import image_link_audit as audit
from soramimic_video.cli import main


@pytest.fixture
def endpoint():
    calls = Counter()
    routes = {
        "/ok": (200, "image/png", b"\x89PNG\r\n\x1a\n"),
        "/missing": (404, "text/html", b"missing"),
        "/gone": (410, "text/html", b"gone"),
        "/denied": (403, "text/html", b"denied"),
        "/limited": (429, "text/html", b"limited"),
        "/error": (503, "text/html", b"error"),
        "/html": (200, "text/html", b"<html>"),
        "/fake": (200, "image/png", b"<!DOCTYPE html>"),
        "/empty": (200, "image/png", b""),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls[self.path] += 1
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
                return
            code, mime, body = routes[self.path]
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", routes, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def write_list(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["original", "image", "image_usage"])
        writer.writeheader()
        writer.writerows(rows)


def test_http_classification_and_redirect(endpoint, monkeypatch):
    base, _, _ = endpoint
    expected = {
        "ok": "ok", "redirect": "ok", "missing": "broken", "gone": "broken",
        "denied": "unavailable", "limited": "unavailable", "error": "unavailable",
        "html": "invalid", "fake": "invalid", "empty": "invalid",
    }
    for path, status in expected.items():
        assert audit.probe(f"{base}/{path}", 1)["status"] == status

    def timeout(*args, **kwargs):
        raise requests.Timeout("test")

    monkeypatch.setattr(audit.requests, "get", timeout)
    assert audit.probe(base, 1)["status"] == "unavailable"


def test_scope_includes_all_affected_names(tmp_path):
    external = "https://example.com/image.png"
    builtin = "https://raw.githubusercontent.com/soramimic/soramimic-wordlists/main/images/a.png"
    rows = [
        {"original": name, "image": url, "image_usage": usage}
        for name, url, usage in [
            ("A", external, "noncommercial_fanwork"),
            ("B", external, "noncommercial_fanwork"),
            ("C", builtin, "noncommercial_fanwork"),
            ("D", "https://example.com/free.png", ""),
        ]
    ]
    write_list(tmp_path / "vtuber.csv", rows)
    links = audit.collect_links(tmp_path, "external-fanwork")
    assert list(links) == [external]
    assert [ref["name"] for ref in links[external]] == ["A", "B"]
    assert len(audit.collect_links(tmp_path, "all")) == 3


def test_report_retry_history_recovery_and_cli(tmp_path, endpoint, monkeypatch):
    base, routes, calls = endpoint
    lists = tmp_path / "lists"
    lists.mkdir()
    write_list(lists / "vtuber.csv", [
        {"original": "A", "image": f"{base}/missing", "image_usage": "noncommercial_fanwork"},
        {"original": "B", "image": f"{base}/missing", "image_usage": "noncommercial_fanwork"},
    ])
    report_dir = tmp_path / "reports"
    monkeypatch.setattr(audit.time, "sleep", lambda _: None)
    args = ["audit-image-links", "--wordlists-dir", str(lists),
            "--report-dir", str(report_dir), "--delay", "0"]
    assert main(args) == 1
    first = json.loads((report_dir / "latest.json").read_text())
    assert first["total"] == 1
    assert first["counts"]["broken"] == 1
    assert calls["/missing"] == 2
    assert len(first["results"][0]["references"]) == 2
    assert main(args) == 1
    second = json.loads((report_dir / "latest.json").read_text())
    assert second["results"][0]["consecutive_failures"] == 2
    routes["/missing"] = routes["/ok"]
    assert main(args) == 0
    third = json.loads((report_dir / "latest.json").read_text())
    assert third["results"][0]["consecutive_failures"] == 0
    assert third["counts"]["ok"] == 1
    assert len(list(report_dir.glob("[0-9]*.json"))) == 3
    # Empty/misconfigured input must not overwrite the last completed audit.
    (lists / "vtuber.csv").unlink()
    assert main(args) == 2
    assert json.loads((report_dir / "latest.json").read_text()) == third


def test_interruption_and_concurrent_run_preserve_report(tmp_path, monkeypatch):
    lists = tmp_path / "lists"
    lists.mkdir()
    write_list(lists / "v.csv", [{"image": "https://example.com/a.png"}])
    reports = tmp_path / "reports"
    reports.mkdir()
    latest = reports / "latest.json"
    latest.write_text('{"results": []}')

    def interrupted(*args):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(audit, "probe", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        audit.audit_links(lists, reports, scope="all")
    assert latest.read_text() == '{"results": []}'
    with audit._store_lock(reports), pytest.raises(RuntimeError, match="実行中"):
        audit.audit_links(lists, reports, scope="all")
