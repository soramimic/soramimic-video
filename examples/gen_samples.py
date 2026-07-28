"""同梱サンプル曲のXF MIDIと元歌詞を生成する。

いずれも詞・曲ともパブリックドメインの童謡・唱歌で、メロディは公知の
楽譜を手打ちしたもの。既存の打ち込みデータは含まない。
1音符=1モーラに正規化してある(合成エンジンが1音符に複数モーラを
載せられないため。「ももたろうさん」→「ももたろさん」は実際の歌い方)。
楽譜のメリスマは「け」→「け・え」のように母音のモーラを足して表す。

曲ごとの権利根拠(作詞・作曲者の没年とPDの理由)は docs/sample-rights.md。
曲を足すときは同じ表にも1行足すこと。

実行: uv run python examples/gen_samples.py
出力: src/soramimic_video/static/sample/<id>.mid / <id>_lyrics.txt / samples.json
(Web UIの「サンプル曲をセット」とAPIの /api/samples, /api/sample/* が配信する)
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack

TPB = 480  # ticks per beat

# 音名 → MIDIノート
A3, AS3, B3 = 57, 58, 59
C4, D4, DS4, E4, F4, FS4, G4, A4, AS4, B4 = 60, 62, 63, 64, 65, 66, 67, 69, 70, 71
C5, D5, DS5, E5 = 72, 74, 75, 76

Q, DQ, E8, DE, S16 = 480, 720, 240, 360, 120  # 4分/付点4分/8分/付点8分/16分
H, DH = 960, 1440  # 2分/付点2分

BR = "/"  # 行区切りだけ入れる(休符は入れない。小節の途中で行が変わる曲用)

# 各曲の "title_kana": 曲名の読み(カタカナ)。サムネの見出しは曲名を空耳変換して
# 作るが、読みをMeCabの推定に任せると「紅葉」が「コーヨー」になってしまう。
# サンプル曲は読みが確定しているのでデータとして持たせ、推定を回避する
# (samples.json に出力し、サーバーが変換入力に使う)。
#
# 各曲の "score": 次の4種類を並べた列。
#   (歌詞かな, MIDIノート, 長さtick) … 1音符=1モーラ
#   int                              … その長さの休符(フレーズ頭の休符など)
#   None                             … 行区切り。次の小節頭まで休符を入れる
#   BR                               … 行区切りのみ(小節の途中で行が変わる曲)
SONGS: dict[str, dict] = {
    "furusato": {
        "title": "ふるさと",
        "title_kana": "フルサト",
        "description": "唱歌・PD",
        "tempo": 600_000,  # ♩=100
        "time": (3, 4),
        # ト長調。「ゆ→ゆ・う」などの2音は楽譜どおりのメリスマ
        "score": [
            ("う", G4, Q), ("さ", G4, Q), ("ぎ", G4, Q),
            ("お", A4, DQ), ("い", B4, E8), ("し", A4, Q),
            ("か", B4, Q), ("の", B4, Q), ("や", C5, Q),
            ("ま", D5, 1200),  # 2拍半(残り半拍は次の行前のブレス休符)
            None,
            ("こ", C5, Q), ("ぶ", D5, Q), ("な", E5, Q),
            ("つ", B4, DQ), ("り", C5, E8), ("し", B4, Q),
            ("か", A4, Q), ("の", A4, Q), ("か", FS4, Q),
            ("わ", G4, 1200),  # 2拍半(残り半拍は次の行前のブレス休符)
            None,
            ("ゆ", A4, E8), ("う", G4, E8), ("め", A4, Q), ("は", D4, Q),
            ("い", G4, E8), ("い", A4, E8), ("ま", B4, Q), ("も", B4, Q),
            ("め", C5, E8), ("え", B4, E8), ("ぐ", C5, DQ), ("う", E5, E8),
            ("り", D5, E8), ("い", C5, E8), ("て", B4, Q),  # 残り1拍は休符
            None,
            ("わ", D5, Q), ("す", D5, Q), ("れ", D5, Q),
            ("が", G4, DQ), ("た", A4, E8), ("き", B4, Q),
            ("ふ", C5, Q), ("る", C5, Q), ("さ", A4, Q),
            ("と", G4, 1440),
        ],
        "lyrics": (
            "うさぎ追いし かの山\n小ぶな釣りし かの川\n夢は今も めぐりて\n忘れがたき ふるさと\n"
        ),
    },
    "akatombo": {
        "title": "赤とんぼ",
        "title_kana": "アカトンボ",
        "description": "童謡・PD",
        "tempo": 666_000,  # ♩≒90
        "time": (3, 4),
        # 変ホ長調。「けぇ」「のぉ」などは楽譜どおりのメリスマ(小書きは大書きに正規化)
        "score": [
            ("ゆ", AS3, E8), ("う", DS4, E8), ("や", DS4, DQ), ("け", F4, E8),
            ("こ", G4, E8), ("や", AS4, E8), ("け", DS5, E8), ("え", C5, E8), ("の", AS4, Q),
            ("あ", C5, E8), ("か", DS4, E8), ("と", DS4, Q),
            ("ん", F4, Q), ("ぼ", G4, 1200),  # 2拍半(残り半拍は次の行前のブレス休符)
            None,
            ("お", G4, E8), ("わ", C5, E8), ("れ", AS4, DQ), ("て", C5, E8),
            ("み", DS5, E8), ("た", C5, E8), ("の", AS4, E8), ("お", C5, E8),
            ("は", AS4, E8), ("あ", G4, E8),
            ("い", AS4, E8), ("つ", G4, E8), ("の", DS4, E8), ("お", G4, E8),
            ("ひ", F4, E8), ("い", DS4, E8),
            ("か", DS4, 1440),
        ],
        "lyrics": "夕焼小焼の 赤とんぼ\n負われて見たのは いつの日か\n",
    },
    "momotarou": {
        "title": "桃太郎",
        "title_kana": "モモタロー",
        "description": "文部省唱歌・PD",
        "tempo": 500_000,  # ♩=120
        "time": (4, 4),
        # ニ長調。「ももたろさん」は歌唱慣行どおり(元歌詞は「桃太郎さん」)
        "score": [
            ("も", A4, DQ), ("も", B4, E8), ("た", A4, E8), ("ろ", A4, E8),
            ("さ", FS4, E8), ("ん", FS4, E8),
            ("も", A4, E8), ("も", A4, E8), ("た", FS4, E8), ("ろ", D4, E8),
            ("さ", E4, Q), ("ん", E4, E8),  # 残り8分は休符
            None,
            ("お", D4, E8), ("こ", D4, E8), ("し", E4, E8), ("に", E4, E8),
            ("つ", FS4, E8), ("け", FS4, E8), ("た", E4, Q),
            ("き", FS4, E8), ("び", FS4, E8), ("だ", B4, E8), ("ん", B4, E8),
            ("ご", A4, DQ),  # 残り8分は休符
            None,
            ("ひ", D5, E8), ("と", D5, E8), ("つ", A4, Q),
            ("わ", FS4, E8), ("た", FS4, E8), ("し", B4, E8), ("に", B4, E8),
            ("く", A4, E8), ("だ", A4, E8), ("さ", FS4, E8), ("い", E4, E8),
            ("な", D4, DQ),
        ],
        "lyrics": "桃太郎さん 桃太郎さん\nお腰につけた きびだんご\n一つわたしに くださいな\n",
    },
    "katatsumuri": {
        "title": "かたつむり",
        "title_kana": "カタツムリ",
        "description": "文部省唱歌・PD",
        "tempo": 500_000,  # ♩=120
        "time": (4, 4),
        # ニ長調・付点のはずむリズム
        "score": [
            ("で", A4, DE), ("ん", A4, S16), ("で", A4, E8), ("ん", FS4, E8),
            ("む", D4, DE), ("し", D4, S16), ("む", D4, E8), ("し", E4, E8),
            ("か", FS4, DE), ("た", FS4, S16), ("つ", E4, E8), ("む", D4, E8),
            ("り", E4, Q),  # 残りは休符
            None,
            ("お", FS4, DE), ("ま", G4, S16), ("え", A4, E8), ("の", B4, E8),
            ("あ", A4, DE), ("た", A4, S16), ("ま", A4, E8), ("は", FS4, E8),
            ("ど", E4, DE), ("こ", E4, S16), ("に", D4, E8), ("あ", E4, E8),
            ("る", FS4, Q),  # 残りは休符
            None,
            ("つ", A4, E8), ("の", D5, E8), ("だ", D5, E8), ("せ", A4, E8),
            ("や", FS4, E8), ("り", A4, E8), ("だ", A4, E8), ("せ", FS4, E8),
            ("あ", D4, E8), ("た", FS4, E8), ("ま", FS4, DE), ("だ", E4, S16),
            ("せ", D4, Q),
        ],
        "lyrics": (
            "でんでんむしむし かたつむり\nお前のあたまは どこにある\nつの出せ槍出せ あたま出せ\n"
        ),
    },
    "harugakita": {
        "title": "春が来た",
        "title_kana": "ハルガキタ",
        "description": "唱歌・PD",
        "tempo": 500_000,  # ♩=120
        "time": (4, 4),
        # ハ長調(初出調)。メリスマ無し・1音符=1モーラの素直な曲
        "score": [
            ("は", G4, Q), ("る", E4, E8), ("が", F4, E8), ("き", G4, Q), ("た", A4, Q),
            ("は", G4, Q), ("る", E4, E8), ("が", F4, E8), ("き", G4, Q), ("た", C5, Q),
            ("ど", A4, Q), ("こ", G4, Q), ("に", E4, DQ), ("き", C4, E8),
            ("た", D4, DH),  # 残り1拍は行末の休符
            None,
            ("や", G4, Q), ("ま", A4, E8), ("に", G4, E8), ("き", E4, Q), ("た", G4, Q),
            ("さ", C5, Q), ("と", D5, E8), ("に", C5, E8), ("き", A4, Q), ("た", C5, Q),
            ("の", G4, Q), ("に", E5, Q), ("も", D5, DQ), ("き", G4, E8),
            ("た", C5, DH),
        ],
        "lyrics": "春が来た 春が来た どこに来た\n山に来た 里に来た 野にも来た\n",
    },
    "oborodukiyo": {
        "title": "朧月夜",
        "title_kana": "オボロヅキヨ",
        "description": "唱歌・PD",
        "tempo": 750_000,  # ♩=80
        "time": (3, 4),
        # ニ長調(初出調)。8分2つの弱起で始まり、行は小節の途中で変わる(BR)。
        # 「け→け・え」などの2音は楽譜どおりのメリスマ
        "score": [
            Q,  # 弱起(3拍子の3拍目から歌い出す)
            ("な", FS4, E8), ("の", FS4, E8),
            ("は", D4, DQ), ("な", E4, E8), ("ば", FS4, E8), ("た", A4, E8),
            ("け", A4, E8), ("え", B4, E8), ("に", A4, Q), ("い", E4, Q),
            ("り", FS4, DQ), ("ひ", D4, E8), ("う", E4, E8), ("す", A4, E8),
            ("れ", FS4, H),
            BR,
            ("み", A4, E8), ("わ", A4, E8),
            ("た", FS4, DQ), ("す", G4, E8), ("や", A4, E8), ("ま", D5, E8),
            ("の", D5, E8), ("お", E5, E8), ("は", D5, Q), ("か", A4, Q),
            ("す", B4, DQ), ("み", FS4, E8), ("ふ", E4, E8), ("か", E4, E8),
            ("し", D4, H),
            BR,
            ("は", A4, E8), ("る", A4, E8),
            ("か", D5, DQ), ("ぜ", D5, E8), ("そ", D5, E8), ("よ", E5, E8),
            ("ふ", D5, E8), ("う", B4, E8), ("く", A4, Q), ("そ", A4, E8), ("お", FS4, E8),
            ("ら", A4, DQ), ("を", B4, E8), ("み", FS4, E8), ("れ", FS4, E8),
            ("ば", E4, H),
            BR,
            ("ゆ", D4, E8), ("う", E4, E8),
            ("づ", FS4, DQ), ("き", D4, E8), ("か", FS4, E8), ("か", G4, E8),
            ("り", A4, E8), ("い", D5, E8), ("て", B4, Q), ("に", A4, Q),
            ("お", B4, DQ), ("い", FS4, E8), ("あ", E4, E8), ("わ", E4, E8),
            ("し", D4, H),
        ],
        "lyrics": (
            "菜の花畑に 入日薄れ\n見わたす山の端 霞ふかし\n"
            "春風そよふく 空を見れば\n夕月かかりて におい淡し\n"
        ),
    },
    "chatsumi": {
        "title": "茶摘",
        "title_kana": "チャツミ",
        "description": "文部省唱歌・PD",
        "tempo": 576_923,  # ♩=104
        "time": (4, 4),
        # ト長調・ヨナ抜き長音階。各行は4分休符から歌い出す。メリスマ無し
        "score": [
            Q,
            ("な", D4, Q), ("つ", G4, Q), ("も", A4, Q),
            ("ち", B4, DQ), ("か", B4, E8), ("づ", B4, Q), ("く", B4, Q),
            ("は", D5, DQ), ("ち", D5, E8), ("じゅ", D5, Q), ("う", B4, Q),
            ("は", A4, Q), ("ち", G4, Q), ("や", A4, Q),  # 残り1拍は行末の休符
            None,
            Q,
            ("の", B4, Q), ("に", B4, Q), ("も", D5, Q),
            ("や", B4, DQ), ("ま", B4, E8), ("に", B4, Q), ("も", A4, Q),
            ("わ", B4, DQ), ("か", B4, E8), ("ば", A4, Q), ("が", G4, Q),
            ("し", E4, Q), ("げ", E4, Q), ("る", D4, Q),
            None,
            Q,
            ("あ", D4, Q), ("れ", G4, Q), ("に", A4, Q),
            ("み", B4, DQ), ("え", B4, E8), ("る", B4, Q), ("は", B4, Q),
            ("ちゃ", D5, DQ), ("つ", D5, E8), ("み", D5, Q), ("じゃ", B4, Q),
            ("な", A4, Q), ("い", G4, Q), ("か", A4, Q),
            None,
            Q,
            ("あ", D5, Q), ("か", D5, Q), ("ね", B4, Q),
            ("だ", A4, DQ), ("す", A4, E8), ("き", G4, Q), ("に", E4, Q),
            ("す", D4, Q), ("げ", G4, Q), ("の", A4, DQ), ("か", B4, E8),
            ("さ", G4, DH),
        ],
        "lyrics": (
            "夏も近づく 八十八夜\n野にも山にも 若葉が茂る\n"
            "あれに見えるは 茶摘じゃないか\nあかねだすきに 菅の笠\n"
        ),
    },
    "nanatsunoko": {
        "title": "七つの子",
        "title_kana": "ナナツノコ",
        "description": "童謡・PD",
        "tempo": 750_000,  # ♩=80
        "time": (4, 4),
        # ト長調。「ら→ら・あ」などの2音は楽譜どおりのメリスマ
        "score": [
            ("か", B4, Q), ("ら", A4, E8), ("あ", G4, E8), ("す", A4, Q),
            ("な", B4, E8), ("ぜ", G4, E8), ("な", E4, E8), ("く", G4, E8), ("の", D4, Q),
            ("か", E4, E8), ("ら", D4, E8), ("す", B3, E8), ("は", D4, E8),
            ("や", E4, Q), ("ま", G4, Q),
            ("に", A4, DH),
            None,
            ("か", B4, DQ), ("あ", C5, E8), ("わ", D5, Q), ("い", B4, Q),
            ("な", D5, Q), ("な", E5, E8), ("あ", D5, E8), ("つ", B4, Q), ("の", G4, Q),
            ("こ", A4, E8), ("が", A4, E8), ("あ", B4, E8), ("る", G4, E8),
            ("か", E4, Q), ("ら", D4, Q),
            ("よ", G4, DH),
            None,
            ("か", A4, Q), ("わ", A4, Q), ("い", A4, Q),
            ("か", A4, DQ), ("わ", A4, E8), ("い", A4, Q), ("と", B4, Q),
            ("か", C5, Q), ("ら", B4, Q), ("す", A4, Q), ("は", A4, Q),
            ("な", A4, Q), ("く", B4, Q), ("の", E4, H),
            None,
            ("か", D4, Q), ("わ", D4, Q), ("い", D4, Q),
            ("か", G4, DQ), ("わ", G4, E8), ("い", G4, Q), ("と", A4, Q),
            ("な", B4, Q), ("く", C5, Q), ("ん", E4, Q), ("だ", G4, Q),
            ("よ", A4, DH),
            None,
            ("や", B4, Q), ("ま", A4, E8), ("あ", G4, E8), ("の", A4, Q),
            ("ふ", B4, E8), ("う", G4, E8), ("る", E4, E8), ("す", G4, E8), ("へ", D4, Q),
            ("い", E4, E8), ("て", D4, E8), ("み", B3, E8), ("て", D4, E8),
            ("ご", E4, Q), ("ら", G4, Q),
            ("ん", A4, DH),
            None,
            ("ま", B4, DQ), ("あ", C5, E8), ("る", D5, Q), ("い", B4, Q),
            ("め", D5, Q), ("を", E5, E8), ("お", D5, E8), ("し", B4, Q), ("た", G4, Q),
            ("い", A4, Q), ("い", B4, E8), ("い", G4, E8), ("こ", E4, Q), ("だ", D4, Q),
            ("よ", G4, DH),
        ],
        "lyrics": (
            "烏 なぜ啼くの 烏は山に\n可愛七つの 子があるからよ\n"
            "可愛 可愛と 烏は啼くの\n可愛 可愛と 啼くんだよ\n"
            "山の古巣へ 行ってみてごらん\n丸い目をした いい子だよ\n"
        ),
    },
    "momiji": {
        "title": "紅葉",
        "title_kana": "モミジ",
        "description": "唱歌・PD",
        "tempo": 652_174,  # ♩=92
        "time": (4, 4),
        # ヘ長調。「る→る・う」などの2音は楽譜どおりのメリスマ
        "score": [
            ("あ", A4, Q), ("き", G4, E8), ("の", F4, E8), ("ゆ", G4, Q), ("う", A4, Q),
            ("ひ", F4, H), ("に", C4, Q),
            ("て", F4, Q), ("る", E4, E8), ("う", F4, E8), ("や", G4, Q), ("ま", C5, Q),
            ("も", A4, Q), ("み", G4, E8), ("い", F4, E8), ("じ", G4, Q),
            None,
            ("こ", A4, Q), ("い", G4, E8), ("も", F4, E8), ("う", G4, Q), ("す", A4, Q),
            ("い", F4, H), ("も", C4, Q),
            ("か", F4, Q), ("ず", E4, E8), ("う", F4, E8), ("あ", G4, Q), ("る", C5, Q),
            ("な", A4, Q), ("か", G4, Q), ("に", F4, Q),
            None,
            ("ま", C5, Q), ("つ", A4, E8), ("を", AS4, E8), ("い", C5, Q), ("ろ", D5, Q),
            ("ど", C5, H), ("る", A4, Q),
            ("か", C5, Q), ("え", D5, E8), ("え", C5, E8), ("で", A4, Q),
            ("や", G4, E8), ("あ", F4, E8),
            ("つ", G4, Q), ("た", A4, Q), ("は", G4, Q),
            None,
            ("や", C5, Q), ("ま", D5, E8), ("の", C5, E8), ("ふ", A4, Q), ("も", G4, Q),
            ("と", F4, H), ("の", C4, Q),
            ("す", F4, Q), ("そ", E4, E8), ("お", F4, E8), ("も", A4, Q), ("よ", G4, Q),
            ("う", F4, DH),
        ],
        "lyrics": (
            "秋の夕日に 照る山紅葉\n濃いも薄いも 数ある中に\n"
            "松をいろどる 楓や蔦は\n山のふもとの 裾模様\n"
        ),
    },
    "shabondama": {
        "title": "しゃぼん玉",
        "title_kana": "シャボンダマ",
        "description": "童謡・PD",
        "tempo": 833_333,  # ♩=72
        "time": (2, 4),
        # ニ長調(初出調)。メリスマ無し・1音符=1モーラ
        "score": [
            ("しゃ", A3, E8), ("ぼ", D4, S16), ("ん", D4, S16), ("だ", D4, E8),
            ("ま", E4, E8), ("と", FS4, E8), ("ん", A4, E8), ("だ", A4, E8),
            None,
            ("や", B4, E8), ("ね", G4, E8), ("ま", D5, E8), ("で", B4, E8),
            ("と", A4, E8), ("ん", B4, E8), ("だ", A4, E8),
            None,
            ("や", FS4, E8), ("ね", FS4, E8), ("ま", E4, E8), ("で", D4, E8),
            ("と", E4, E8), ("ん", A4, E8), ("で", A4, E8),
            None,
            ("こ", B4, E8), ("わ", B4, E8), ("れ", A4, E8), ("て", D4, E8),
            ("き", FS4, E8), ("え", E4, E8), ("た", D4, E8),
        ],
        "lyrics": "しゃぼん玉飛んだ\n屋根まで飛んだ\n屋根まで飛んで\nこわれて消えた\n",
    },
}


def _track_chunk_bytes(mid: MidiFile) -> bytes:
    buf = io.BytesIO()
    mid.save(file=buf)
    data = buf.getvalue()
    start = data.index(b"MTrk")
    return data[start:]


def build(song: dict) -> bytes:
    num, den = song["time"]
    meas = TPB * num * 4 // den
    lead_in = meas  # 1小節ぶんの前奏(無音)
    notes: list[tuple[int, int, int]] = []  # (start, dur, note)
    lyric_events: list[tuple[int, str]] = []  # (tick, text)
    tick = lead_in
    line_break = False
    for item in song["score"]:
        if item is None:
            line_break = True
            tick += -tick % meas  # 次の小節頭まで進める(行末の休符)
            continue
        if item == BR:  # 小節の途中で行が変わる曲(朧月夜など)。休符は入れない
            line_break = True
            continue
        if isinstance(item, int):  # 休符
            tick += item
            continue
        kana, note, dur = item
        notes.append((tick, dur, note))
        lyric_events.append((tick, ("/" if line_break else "") + kana))
        line_break = False
        tick += dur

    mid = MidiFile(ticks_per_beat=TPB, charset="cp932")
    track = MidiTrack()
    mid.tracks.append(track)
    track.append(MetaMessage("set_tempo", tempo=song["tempo"], time=0))
    track.append(MetaMessage("time_signature", numerator=num, denominator=den, time=0))
    events: list[tuple[int, Message]] = []
    for start, dur, note in notes:
        events.append((start, Message("note_on", channel=0, note=note, velocity=100, time=0)))
        events.append((start + dur, Message("note_off", channel=0, note=note, velocity=64, time=0)))
    events.sort(key=lambda e: e[0])
    prev = 0
    for t, msg in events:
        msg.time = t - prev
        track.append(msg)
        prev = t
    track.append(MetaMessage("end_of_track", time=0))

    # XFIH + XFKM チャンク(tests/helpers.py と同じ合成方法)
    ih = MidiFile(ticks_per_beat=TPB, charset="cp932")
    iht = MidiTrack()
    ih.tracks.append(iht)
    iht.append(MetaMessage("cue_marker", text="$XFhd:", time=0))
    iht.append(MetaMessage("end_of_track", time=0))
    xfih = _track_chunk_bytes(ih).replace(b"MTrk", b"XFIH", 1)

    xf = MidiFile(ticks_per_beat=TPB, charset="cp932")
    xft = MidiTrack()
    xf.tracks.append(xft)
    xft.append(MetaMessage("cue_marker", text="$Lyrc:1:0:JP", time=0))
    prev = 0
    for t, text in lyric_events:
        xft.append(MetaMessage("lyrics", text=text, time=t - prev))
        prev = t
    xft.append(MetaMessage("end_of_track", time=0))
    xfkm = _track_chunk_bytes(xf).replace(b"MTrk", b"XFKM", 1)

    buf = io.BytesIO()
    mid.save(file=buf)
    return buf.getvalue() + xfih + xfkm


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "src" / "soramimic_video" / "static" / "sample"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for sid, song in SONGS.items():
        data = build(song)
        (out_dir / f"{sid}.mid").write_bytes(data)
        (out_dir / f"{sid}_lyrics.txt").write_text(song["lyrics"], encoding="utf-8")
        manifest.append(
            {
                "id": sid,
                "title": song["title"],
                "title_kana": song["title_kana"],
                "description": song["description"],
            }
        )
        print(f"wrote {sid}.mid ({len(data)} bytes)")
    (out_dir / "samples.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote samples.json ({len(manifest)} songs)")
