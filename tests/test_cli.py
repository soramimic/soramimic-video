from soramimic_video import cli
from soramimic_video.cli import build_parser


def test_video_image_lead_defaults_and_can_be_disabled():
    parser = build_parser()

    video = parser.parse_args(["video", "--project", "work/song"])
    assert video.image_lead_sec == 0.1
    disabled = parser.parse_args([
        "video", "--project", "work/song", "--image-lead-sec", "0",
    ])
    assert disabled.image_lead_sec == 0.0
    assert video.noncommercial_fanwork is False
    allowed = parser.parse_args([
        "video", "--project", "work/song", "--noncommercial-fanwork",
    ])
    assert allowed.noncommercial_fanwork is True

    serve = parser.parse_args(["serve"])
    assert serve.video_image_lead_sec == 0.1
    assert serve.serial_video is False
    serve_disabled = parser.parse_args(["serve", "--video-image-lead-sec", "0"])
    assert serve_disabled.video_image_lead_sec == 0.0
    assert parser.parse_args(["serve", "--serial-video"]).serial_video is True


def test_serve_preserves_the_immediate_socket_peer(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(["serve", "--jobs-dir", str(tmp_path / "jobs")])
    seen = {}
    monkeypatch.setattr("soramimic_video.api.create_app", lambda **kwargs: object())
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: seen.update(kwargs))

    assert cli.cmd_serve(args) == 0
    assert seen["proxy_headers"] is False
