from soramimic_video.cli import build_parser


def test_video_image_lead_defaults_and_can_be_disabled():
    parser = build_parser()

    video = parser.parse_args(["video", "--project", "work/song"])
    assert video.image_lead_sec == 0.1
    disabled = parser.parse_args([
        "video", "--project", "work/song", "--image-lead-sec", "0",
    ])
    assert disabled.image_lead_sec == 0.0

    serve = parser.parse_args(["serve"])
    assert serve.video_image_lead_sec == 0.1
    serve_disabled = parser.parse_args(["serve", "--video-image-lead-sec", "0"])
    assert serve_disabled.video_image_lead_sec == 0.0
