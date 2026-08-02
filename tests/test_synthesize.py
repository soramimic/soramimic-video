import xml.etree.ElementTree as ET
from pathlib import Path

from helpers import build_xf_midi
from soramimic_video import synthesize as synth_mod
from soramimic_video.musicxml import build_musicxml
from soramimic_video.project import Parody, ParodyLine, ParodyWord
from soramimic_video.synthesize import build_lyric_map, synthesize
from soramimic_video.xfparse import analyze_midi


def _project(tmp_path: Path):
    midi = build_xf_midi(
        tmp_path / "song.mid",
        notes=[(480, 240, 60), (720, 240, 62), (960, 1200, 64)],  # 3音符目は小節をまたぐ
        lyric_events=[(480, "沈[し"), (720, "ず]"), (960, "む")],
    )
    return analyze_midi(midi)


def _high_project(tmp_path: Path):
    # NEUTRINO音域(50〜74)より高い音符ばかりの曲。自動オクターブ調整が-12を選ぶ。
    midi = build_xf_midi(
        tmp_path / "high.mid",
        notes=[(480, 240, 80), (720, 240, 82), (960, 1200, 84)],
        lyric_events=[(480, "た"), (720, "か"), (960, "い")],
    )
    return analyze_midi(midi)


def _wide_project(tmp_path: Path):
    """音域が広くどのオクターブにも収まらない曲(61〜83。女々しくて相当)。"""
    pitches = list(range(61, 84))
    return analyze_midi(
        build_xf_midi(
            tmp_path / "wide.mid",
            notes=[(240 * i, 240, p) for i, p in enumerate(pitches)],
            lyric_events=[(240 * i, "ラ") for i in range(len(pitches))],
        )
    )


def _capture_neutrino(monkeypatch):
    """NEUTRINOバイナリを実行せず、build_musicxmlに渡ったtransposeを捕捉する。"""
    captured: dict[str, int] = {}
    orig = synth_mod.build_musicxml

    def fake_build_musicxml(project, lyric_map, transpose=0):
        captured["transpose"] = transpose
        return orig(project, lyric_map, transpose=transpose)

    monkeypatch.setattr(synth_mod, "build_musicxml", fake_build_musicxml)
    monkeypatch.setattr(
        synth_mod, "run_neutrino", lambda *a, **k: k.get("work_dir")
    )
    # 既定では汎用音域にフォールバックさせる(実環境のNEUTRINO_ROOTに依存しない)。
    # モデル推奨音域を使うテストは個別に model_pitch_range を差し替える。
    monkeypatch.setattr(synth_mod, "model_pitch_range", lambda model: None)
    return captured


def test_neutrino_auto_octave_adds_shift_to_transpose(tmp_path: Path, monkeypatch):
    project = _high_project(tmp_path)
    captured = _capture_neutrino(monkeypatch)
    synthesize(project, tmp_path, synthesizer="neutrino", transpose=0, auto_octave=True)
    # 80〜84は-12で68〜72になり音域内。transposeに-12が加算される。
    assert captured["transpose"] == -12


def test_neutrino_auto_octave_off_keeps_transpose(tmp_path: Path, monkeypatch):
    project = _high_project(tmp_path)
    captured = _capture_neutrino(monkeypatch)
    synthesize(project, tmp_path, synthesizer="neutrino", transpose=-3, auto_octave=False)
    # 自動調整OFFならユーザー指定transposeがそのまま渡る
    assert captured["transpose"] == -3


def test_neutrino_auto_octave_adds_on_top_of_user_transpose(
    tmp_path: Path, monkeypatch
):
    project = _high_project(tmp_path)
    captured = _capture_neutrino(monkeypatch)
    # ユーザーが+12した上でONにすると、80〜84+12=92〜96はさらに-24で収まる
    synthesize(project, tmp_path, synthesizer="neutrino", transpose=12, auto_octave=True)
    assert captured["transpose"] == 12 - 24


def test_neutrino_auto_octave_uses_model_range(tmp_path: Path, monkeypatch):
    project = _high_project(tmp_path)  # 音符 80〜84
    captured = _capture_neutrino(monkeypatch)
    # モデル推奨音域が高め(A4〜C6 = 69〜84)なら 80〜84 はそのまま収まりシフトなし
    monkeypatch.setattr(synth_mod, "model_pitch_range", lambda model: (69, 84))
    synthesize(project, tmp_path, synthesizer="neutrino", transpose=0, auto_octave=True)
    assert captured["transpose"] == 0


def test_neutrino_auto_octave_falls_back_to_generic_range(tmp_path: Path, monkeypatch):
    project = _high_project(tmp_path)
    captured = _capture_neutrino(monkeypatch)
    # 取得失敗(None)なら汎用音域50〜74で調整され、80〜84は-12
    monkeypatch.setattr(synth_mod, "model_pitch_range", lambda model: None)
    synthesize(project, tmp_path, synthesizer="neutrino", transpose=0, auto_octave=True)
    assert captured["transpose"] == -12


def test_neutrino_auto_octave_uses_given_keys(tmp_path: Path, monkeypatch):
    """プレビューは切り出した音符でなく渡された音域(曲全体)でキーを決める。"""
    project = _project(tmp_path)  # 音符 60〜64。単体では汎用音域50〜74に収まる
    captured = _capture_neutrino(monkeypatch)
    synthesize(
        project, tmp_path, synthesizer="neutrino", transpose=0, auto_octave=True,
        octave_keys=[80, 82, 84],
    )
    assert captured["transpose"] == -12


def test_neutrino_key_shift_is_recorded_on_project(tmp_path: Path, monkeypatch):
    """オクターブで収まらない曲はキー変更が入り、伴奏用に project へ記録される。"""
    project = _wide_project(tmp_path)  # 61〜83
    captured = _capture_neutrino(monkeypatch)
    synthesize(project, tmp_path, synthesizer="neutrino", transpose=0, auto_octave=True)
    # NEUTRINO汎用音域50〜74には -11半音(+1半音 & -1オクターブ)で全音符が収まる
    assert captured["transpose"] == -11
    assert project.song.key_shift == 1


def test_neutrino_key_shift_zero_for_octave_only_song(tmp_path: Path, monkeypatch):
    """オクターブだけで収まる曲はキー変更なし(既存曲の挙動は不変)。"""
    project = _high_project(tmp_path)
    _capture_neutrino(monkeypatch)
    synthesize(project, tmp_path, synthesizer="neutrino", transpose=0, auto_octave=True)
    assert project.song.key_shift == 0


def test_auto_octave_off_resets_key_shift(tmp_path: Path, monkeypatch):
    """自動調整OFFなら歌は原調なので、伴奏のキー変更も解除する。"""
    project = _wide_project(tmp_path)
    project.song.key_shift = -5  # 前回実行の残り
    captured = _capture_neutrino(monkeypatch)
    synthesize(project, tmp_path, synthesizer="neutrino", transpose=0, auto_octave=False)
    assert captured["transpose"] == 0
    assert project.song.key_shift == 0


def test_build_lyric_map_defaults_to_original(tmp_path: Path):
    project = _project(tmp_path)
    assert build_lyric_map(project) == {0: "シ", 1: "ズ", 2: "ム"}


def test_build_lyric_map_with_parody(tmp_path: Path):
    project = _project(tmp_path)
    project.parody = Parody(
        wordlist="test",
        lines=[
            ParodyLine(
                line_id=0,
                words=[
                    ParodyWord(
                        surface="静", kana="シズオ", original="静",
                        original_surface="シズム", originalkana="シズム",
                        note_ids=[0, 1, 2], note_kana=["シ", "ズ", "オ"],
                    )
                ],
            )
        ],
    )
    assert build_lyric_map(project) == {0: "シ", 1: "ズ", 2: "オ"}


def test_build_lyric_map_warns_on_double_assignment(tmp_path: Path, caplog):
    # 同じ音符(id=2)を2単語が取ると後勝ち上書きになる。修正後の変換では
    # 起きないはずだが、将来の同種バグ検出のため警告を出すことを確認する。
    project = _project(tmp_path)
    project.parody = Parody(
        wordlist="test",
        lines=[
            ParodyLine(
                line_id=0,
                words=[
                    ParodyWord(
                        surface="アロ", kana="アロ", original="",
                        original_surface="", originalkana="",
                        note_ids=[0, 1, 2], note_kana=["ア", "ロ", "ガ"],
                    ),
                    ParodyWord(
                        surface="オス", kana="オス", original="",
                        original_surface="", originalkana="",
                        note_ids=[2], note_kana=["オ"],
                    ),
                ],
            )
        ],
    )
    with caplog.at_level("WARNING", logger="soramimic_video.synthesize"):
        lyric_map = build_lyric_map(project)
    assert lyric_map[2] == "オ"  # 後勝ち(既存挙動は不変)
    assert any(
        "音符2" in r.getMessage() and "アロ" in r.getMessage() and "オス" in r.getMessage()
        for r in caplog.records
    )


def test_build_musicxml(tmp_path: Path):
    project = _project(tmp_path)
    xml = build_musicxml(project, build_lyric_map(project))
    assert "<divisions>480</divisions>" in xml
    assert "<text>シ</text>" in xml
    assert "<rest />" in xml  # 曲頭の休符
    # 3音符目(tick960〜2160)は小節境界(1920)をまたぐのでタイが付く
    assert '<tie type="start" />' in xml
    assert '<tie type="stop" />' in xml
    # タイの後半に歌詞は付かない(「ム」は1回だけ)
    assert xml.count("<text>ム</text>") == 1


def test_build_musicxml_transpose(tmp_path: Path):
    project = _project(tmp_path)
    xml = build_musicxml(project, {})
    down = build_musicxml(project, {}, transpose=-12)
    # 音名は同じままオクターブだけ1つ下がる
    for line, line_down in zip(xml.splitlines(), down.splitlines(), strict=True):
        if "<octave>" in line:
            octave = int(line.strip().removeprefix("<octave>").removesuffix("</octave>"))
            octave_down = int(
                line_down.strip().removeprefix("<octave>").removesuffix("</octave>")
            )
            assert octave_down == octave - 1
    assert "<octave>" in xml  # 比較対象が実在すること


def test_build_musicxml_tempo(tmp_path: Path):
    project = _project(tmp_path)
    xml = build_musicxml(project, {})
    assert '<sound tempo="120' in xml  # 500000us/beat = 120bpm


def _zero_start_project(tmp_path: Path):
    """曲頭(tick 0)から音符が始まる曲。"""
    midi = build_xf_midi(
        tmp_path / "zero.mid",
        notes=[(0, 480, 60), (480, 480, 62)],
        lyric_events=[(0, "し"), (480, "ず")],
    )
    return analyze_midi(midi)


def _durations(xml: str) -> list[tuple[bool, int]]:
    """(休符か, duration) の並び。タイ・小節分割後の生の並びを見る。"""
    root = ET.fromstring(xml)
    return [
        (n.find("rest") is not None, int(n.find("duration").text))
        for n in root.iter("note")
    ]


def test_build_musicxml_head_rest_when_song_starts_at_zero(tmp_path: Path):
    # NEUTRINOは休符始まりでないスコアの頭に2秒の休符を勝手に足して歌を遅らせるため、
    # 曲頭から音符が始まる曲では自前で先頭に休符を置く
    project = _zero_start_project(tmp_path)
    xml = build_musicxml(project, build_lyric_map(project))
    segs = _durations(xml)
    assert segs[0][0] is True  # 先頭は休符
    lead = round(0.03 * 960)  # HEAD_REST_SEC × 960tick/秒(480tpb・120bpm)
    assert segs[0][1] == lead
    # 休符は「足す」のではなく先頭音符から借りる(2音目以降の位置がずれない)
    assert segs[1] == (False, 480 - lead)
    assert segs[0][1] + segs[1][1] == 480  # 2音目は元どおりtick480から始まる
    assert sum(d for _, d in segs) == 1920  # 曲全体(小節末までの休符埋め)も変わらない


def test_build_musicxml_head_rest_scales_with_tempo(tmp_path: Path):
    # 先頭休符は秒で決めるのでテンポが変わればtick数も変わる(240bpmなら半分)
    project = _zero_start_project(tmp_path)
    project.song.tempo_map = [[0, 250_000]]  # 240bpm
    segs = _durations(build_musicxml(project, {}))
    assert segs[0] == (True, round(0.03 * 1920))


def test_build_musicxml_keeps_head_rest_of_normal_song(tmp_path: Path):
    # 曲頭に隙間がある通常の曲(1拍目が休符)は何も変えない
    project = _project(tmp_path)
    segs = _durations(build_musicxml(project, {}))
    assert segs[0] == (True, 480)
