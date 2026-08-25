from soramimic_video import asset_store, cli
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
    assert parser.parse_args(["serve"]).asset_store is None


def test_serve_preserves_the_immediate_socket_peer(monkeypatch, tmp_path):
    parser = build_parser()
    args = parser.parse_args(["serve", "--jobs-dir", str(tmp_path / "jobs")])
    seen = {}
    monkeypatch.setattr("soramimic_video.api.create_app", lambda **kwargs: object())
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: seen.update(kwargs))

    assert cli.cmd_serve(args) == 0
    assert seen["proxy_headers"] is False


def test_serve_configures_and_validates_asset_store(monkeypatch, tmp_path):
    store = tmp_path / "assets"
    store.mkdir()
    (store / "manifest.json").write_text(
        '{"version": 1, "assets": {"https://example.com/a.png": {}}}',
        encoding="utf-8",
    )
    args = build_parser().parse_args([
        "serve", "--jobs-dir", str(tmp_path / "jobs"),
        "--asset-store", str(store),
    ])
    monkeypatch.setattr("soramimic_video.api.create_app", lambda **kwargs: object())
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    assert cli.cmd_serve(args) == 0
    assert asset_store.configured_asset_store() == store.resolve()


def test_serve_rejects_asset_store_without_manifest(tmp_path):
    args = build_parser().parse_args([
        "serve", "--asset-store", str(tmp_path / "missing"),
    ])

    assert cli.cmd_serve(args) == 2


def test_serve_rejects_invalid_configured_asset_store(monkeypatch, tmp_path):
    store = tmp_path / "assets"
    store.mkdir()
    (store / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv(asset_store.ASSET_STORE_ENV, str(store))
    args = build_parser().parse_args(["serve"])

    assert cli.cmd_serve(args) == 2
