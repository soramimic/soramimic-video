"""同梱サンプル曲のXF MIDIと元歌詞を生成する。

いずれも詞・曲ともパブリックドメインの童謡・唱歌(および英語圏のPD曲)で、
メロディは公知の楽譜を手打ちしたもの。既存の打ち込みデータは含まない。
1音符=1モーラに正規化してある(合成エンジンが1音符に複数モーラを
載せられないため。「ももたろうさん」→「ももたろさん」は実際の歌い方)。
楽譜のメリスマは「け」→「け・え」のように母音のモーラを足して表す。
英語詞の曲はXFの表記に英語原詞、読みにカタカナを持たせる。1音節が
複数モーラになる箇所は音符を分割して割り付ける(「star」→ ス・タ・ー)。

メロディ(ch0)のほかに伴奏(ch1)も書き込む。ミックス工程(mix.py)は
メロディチャンネルのnoteだけを消してfluidsynthに渡すので、伴奏を別チャンネルに
置かないとカラオケ音源が無音になる。伴奏は曲ごとの "chords" から機械的に作る。

曲ごとの権利根拠(作詞・作曲者の没年とPDの理由)は docs/sample-rights.md。
曲を足すときは同じ表にも1行足すこと。

実行: uv run python examples/gen_samples.py
出力: src/soramimic_video/static/sample/<id>[_full].mid /
      <id>[_full]_lyrics.txt / samples.json
(Web UIの「サンプル曲をセット」とAPIの /api/samples, /api/sample/* が配信する)
"""

from __future__ import annotations

import io
import json
import re
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

MELODY_CHANNEL = 0
ACC_CHANNEL = 1  # 伴奏。mix.py はメロディchのnoteだけ消すので別chに置く
ACC_PROGRAM = 0  # Acoustic Grand Piano

# コードの構成音(ルートからの半音)
_QUALITIES = {"": (0, 4, 7), "m": (0, 3, 7), "7": (0, 4, 7, 10), "m7": (0, 3, 7, 10)}
_ROOTS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_CHORD_RE = re.compile(r"([A-G])([b#]?)(m7|m|7)?$")

CHORD_LOW = 48  # ブロックコードのルートを置く帯域の下端(C3)。メロディ(C4〜E5)の下
BASS_LOW = 36  # ベース音の帯域の下端(C2)
# ベロシティは強め。mix.py の伴奏ゲインは ACCOMPANIMENT_GAIN_MAX(0.6)で頭打ちなので、
# 伴奏wavが歌唱wavより十分大きく鳴っていないと、ミックス後に伴奏が埋もれてしまう
# (FluidR3_GMで -19 LUFS 前後 / true peak -5dB 前後になる値)
CHORD_VELOCITY = 100
BASS_VELOCITY = 112
ACC_GATE = 0.9  # 各打鍵の長さ(拍に対する比率)。少し切って刻みを聞こえやすくする

# 拍子(分子)ごとの刻み。"bass"=ルート低音 / "fifth"=5度の低音 / "chord"=ブロックコード
BEAT_PATTERNS = {
    2: ("bass", "chord"),
    3: ("bass", "chord", "chord"),  # ワルツ(ズン・チャッ・チャッ)
    4: ("bass", "chord", "fifth", "chord"),
}

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
# 英語曲の "xf_words" は行ごとの (英語表記, 歌唱モーラ数)。score のカタカナと
# 組み合わせ、XFKMに `Amazing[ア` `メ` ... `グ]` と書き出す。
#
# 各曲の "chords": 伴奏のコード進行。1要素=1小節で、先頭は前奏(無音の1小節)ぶん。
# 小節内で変えたいときは tuple にする(小節を均等割りするので、要素数は拍子の
# 分子を割り切れること: 4/4なら1・2・4個、3/4なら1・3個、2/4なら1・2個)。
# 小節数は score から計算した値と一致していないと build() が落ちる(ずれ検知)。


def kana_notes(text: str) -> tuple[str, ...]:
    """空白区切りの歌唱カナを、1音符ずつの列にする。"""
    return tuple(text.split())


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
        "additional_verses": [
            kana_notes(
                "い か に い ま す ち ち は は "
                "つ つ が な し や と も が き "
                "あ あ め に か あ ぜ に つ う け え て え も "
                "お も い い ず る ふ る さ と"
            ),
            kana_notes(
                "こ こ ろ ざ し を は た し て "
                "い つ の ひ に か か え ら ん "
                "や あ ま は あ あ お き ふ う る う さ あ と "
                "み ず は き よ き ふ る さ と"
            ),
        ],
        # ト長調。前奏1小節 + 16小節(4小節×4行)
        "chords": [
            "G",
            "G", "D7", ("G", "G", "C"), "G",
            "C", "G", "D7", "G",
            "D7", "G", "C", "G",
            "G", "Em", "D7", "G",
        ],
        "lyrics": (
            "うさぎ追いし かの山\n小ぶな釣りし かの川\n夢は今も めぐりて\n忘れがたき ふるさと\n"
            "いかにいます 父母\nつつがなしや 友がき\n雨に風につけても\n思いいずる ふるさと\n"
            "志を果たして\nいつの日にか 帰らん\n山は青き ふるさと\n水は清き ふるさと\n"
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
        "additional_verses": [
            kana_notes(
                "や ま の は た け え の お く わ の み を "
                "こ か ご に つ ん だ あ は あ ま ぼ ろ お し い か"
            ),
            kana_notes(
                "じゅ う ご で ね え や あ は よ め に ゆ き "
                "お さ と の た よ り い も お た え は あ て え た"
            ),
            kana_notes(
                "ゆ う や け こ や け え の あ か と ん ぼ "
                "と ま っ て い る よ お よ お さ お の お さ あ き"
            ),
        ],
        # 変ホ長調。前奏1小節 + 8小節
        "chords": [
            "Eb",
            "Eb", "Eb", "Ab", "Eb",
            "Eb", "Cm", ("Eb", "Eb", "Bb7"), "Eb",
        ],
        "lyrics": (
            "夕焼小焼の 赤とんぼ\n負われて見たのは いつの日か\n"
            "山の畑の 桑の実を\n小籠に摘んだは まぼろしか\n"
            "十五で姐やは 嫁に行き\nお里のたよりも 絶えはてた\n"
            "夕焼小焼の 赤とんぼ\nとまっているよ 竿の先\n"
        ),
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
        "additional_verses": [
            kana_notes(
                "や あ り ま しょ う や あ り ま しょ う "
                "こ れ か ら お に の せ い ば つ に "
                "つ い て い く な ら や り ま しょ う"
            ),
            kana_notes(
                "い い き ま しょ う い い き ま しょ う "
                "あ な た に つ い て ど こ ま で も "
                "け ら い に な っ て い き ま しょ う"
            ),
            kana_notes(
                "そ お りゃ す す め そ お りゃ す す め "
                "い ち ど に せ め て せ め や ぶ り "
                "つ ぶ し て し ま え お に が し ま"
            ),
            kana_notes(
                "お お も し ろ い お お も し ろ い "
                "の こ ら ず お に を せ め ふ せ て "
                "ぶ ん ど り も の を え ん や ら や"
            ),
            kana_notes(
                "ば ん ば ん ざ い ば ん ば ん ざ い "
                "お と も の い ぬ や さ る き じ は "
                "い さ ん で く る ま え ん や ら や"
            ),
        ],
        # ニ長調。前奏1小節 + 6小節
        "chords": [
            "D",
            "D", ("D", "A7"), "D", ("D", "A7"), "D", ("A7", "D"),
        ],
        "lyrics": (
            "桃太郎さん 桃太郎さん\nお腰につけた きびだんご\n一つわたしに くださいな\n"
            "やりましょう やりましょう\nこれから鬼の 征伐に\nついて行くなら やりましょう\n"
            "行きましょう 行きましょう\nあなたについて どこまでも\n家来になって 行きましょう\n"
            "そりゃ進め そりゃ進め\n一度に攻めて 攻めやぶり\nつぶしてしまえ 鬼ヶ島\n"
            "おもしろい おもしろい\n残らず鬼を 攻めふせて\nぶんどりものを エンヤラヤ\n"
            "万々歳 万々歳\nお供の犬や猿キジは\n勇んで車を エンヤラヤ\n"
        ),
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
        "additional_verses": [
            kana_notes(
                "で ん で ん む し む し か た つ む り "
                "お ま え の め だ ま は ど こ に あ る "
                "つ の だ せ や り だ せ め だ ま だ せ"
            ),
        ],
        # ニ長調。前奏1小節 + 6小節
        "chords": [
            "D",
            "D", ("D", "A7"), "D", ("A7", "D"), "D", "D",
        ],
        "lyrics": (
            "でんでんむしむし かたつむり\nお前のあたまは どこにある\nつの出せ槍出せ あたま出せ\n"
            "でんでんむしむし かたつむり\nお前のめだまは どこにある\nつの出せ槍出せ めだま出せ\n"
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
        "additional_verses": [
            kana_notes(
                "は な が さ く は な が さ く ど こ に さ く "
                "や ま に さ く さ と に さ く の に も さ く"
            ),
            kana_notes(
                "と り が な く と り が な く ど こ で な く "
                "や ま で な く さ と で な く の で も な く"
            ),
        ],
        # ハ長調。前奏1小節 + 8小節
        "chords": [
            "C",
            "C", "C", ("Am", "C"), "G7",
            "C", ("C", "F"), ("C", "G7"), "C",
        ],
        "lyrics": (
            "春が来た 春が来た どこに来た\n山に来た 里に来た 野にも来た\n"
            "花が咲く 花が咲く どこに咲く\n山に咲く 里に咲く 野にも咲く\n"
            "鳥が鳴く 鳥が鳴く どこで鳴く\n山で鳴く 里で鳴く 野でも鳴く\n"
        ),
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
            H,  # 弱起(3拍子の3拍目から歌い出すので、小節頭から2拍ぶん空ける)
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
        "additional_verses": [
            kana_notes(
                "さ と わ の ほ か げ え も も り の い ろ も "
                "た な か の こ み ち い を た ど る ひ と も "
                "か わ ず の な く ね え も か あ ね の お と も "
                "さ な が ら か す め る お ぼ ろ づ き よ お"
            ),
        ],
        # ニ長調。前奏1小節 + 弱起の小節 + 16小節(最後の小節は2拍で終わる)
        "chords": [
            "D",
            "D",
            "D", "A7", "D", "D",
            "D", ("D", "D", "A7"), ("Bm", "Bm", "A7"), "D",
            "D", "D", "D", "A7",
            "D", "D", ("Bm", "Bm", "A7"), "D",
        ],
        "lyrics": (
            "菜の花畑に 入日薄れ\n見わたす山の端 霞ふかし\n"
            "春風そよふく 空を見れば\n夕月かかりて におい淡し\n"
            "里わの火影も 森の色も\n田中の小路を たどる人も\n"
            "蛙のなくねも かねの音も\nさながら霞める 朧月夜\n"
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
        "additional_verses": [
            kana_notes(
                "ひ よ り つ づ き の きょ う こ の ご ろ を "
                "こ こ ろ の ど か に つ み つ つ う た う "
                "つ め よ つ め つ め つ ま ね ば な ら ぬ "
                "つ ま にゃ に ほ ん の ちゃ に な ら ぬ"
            ),
        ],
        # ト長調。前奏1小節 + 16小節(4小節×4行)
        "chords": [
            "G",
            "G", "G", "G", ("G", "D7"),
            "G", "G", "G", ("Em", "G"),
            "G", "G", "G", ("G", "D7"),
            "G", ("D7", "G"), ("G", "D7"), "G",
        ],
        "lyrics": (
            "夏も近づく 八十八夜\n野にも山にも 若葉が茂る\n"
            "あれに見えるは 茶摘じゃないか\nあかねだすきに 菅の笠\n"
            "日和つづきの 今日この頃を\n心のどかに 摘みつつ歌う\n"
            "摘めよ摘め摘め 摘まねばならぬ\n摘まにゃ日本の 茶にならぬ\n"
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
        # ト長調。前奏1小節 + 24小節(4小節×6行)
        "chords": [
            "G",
            "G", ("Em", "G"), ("Em", "D7"), "D7",
            "G", "G", ("G", "D7"), "G",
            "D7", ("D7", "D7", "G", "C"), ("G", "D7"), "Em",
            "G", "G", ("C", "C", "G", "D7"), "D7",
            "G", ("Em", "G"), ("Em", "D7"), "D7",
            "G", "G", ("G", "D7"), "G",
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
        "tempo": 652_174,  # ♩=92(うたごえサークルおけらの譜面・mu-techのXF MIDIともに♩=92)
        "time": (4, 4),
        # ヘ長調・4/4・全16小節。「る→る・う」などの2音は楽譜どおりのメリスマ。
        # 各行の2小節目は「ひ(2分)に(4分)+4分休符」で1小節ちょうど。この休符を
        # 落とすと以降が1拍前にずれて小節線と合わなくなる(リズムが変に聞こえる)
        "score": [
            ("あ", A4, Q), ("き", G4, E8), ("の", F4, E8), ("ゆ", G4, Q), ("う", A4, Q),
            ("ひ", F4, H), ("に", C4, Q), Q,
            ("て", F4, Q), ("る", E4, E8), ("う", F4, E8), ("や", G4, Q), ("ま", C5, Q),
            ("も", A4, Q), ("み", G4, E8), ("い", F4, E8), ("じ", G4, Q),
            None,
            ("こ", A4, Q), ("い", G4, E8), ("も", F4, E8), ("う", G4, Q), ("す", A4, Q),
            ("い", F4, H), ("も", C4, Q), Q,
            ("か", F4, Q), ("ず", E4, E8), ("う", F4, E8), ("あ", G4, Q), ("る", C5, Q),
            ("な", A4, Q), ("か", G4, Q), ("に", F4, Q),
            None,
            ("ま", C5, Q), ("つ", A4, E8), ("を", AS4, E8), ("い", C5, Q), ("ろ", D5, Q),
            ("ど", C5, H), ("る", A4, Q), Q,
            ("か", C5, Q), ("え", D5, E8), ("え", C5, E8), ("で", A4, Q),
            ("や", G4, E8), ("あ", F4, E8),
            ("つ", G4, Q), ("た", A4, Q), ("は", G4, Q),
            None,
            ("や", C5, Q), ("ま", D5, E8), ("の", C5, E8), ("ふ", A4, Q), ("も", G4, Q),
            ("と", F4, H), ("の", C4, Q), Q,
            ("す", F4, Q), ("そ", E4, E8), ("お", F4, E8), ("も", A4, Q), ("よ", G4, Q),
            ("う", F4, DH),
        ],
        "additional_verses": [
            kana_notes(
                "た に の な が れ に ち り い う く も み い じ "
                "な み い に ゆ ら れ て は な れ て よ っ て "
                "あ か や き い ろ の い ろ お さ ま あ ざ ま に "
                "み ず の う え に も お る う に し き"
            ),
        ],
        # ヘ長調。前奏1小節 + 16小節(4小節×4行)
        "chords": [
            "F",
            "F", ("F", "C7"), "F", ("Dm", "C7"),
            "F", ("F", "C7"), "F", ("Dm", "F"),
            ("F", "Bb"), ("C7", "F"), "F", ("Dm", "C7"),
            "F", ("F", "C7"), ("F", "F", "F", "C7"), "F",
        ],
        "lyrics": (
            "秋の夕日に 照る山紅葉\n濃いも薄いも 数ある中に\n"
            "松をいろどる 楓や蔦は\n山のふもとの 裾模様\n"
            "谷の流れに 散り浮く紅葉\n波にゆられて 離れて寄って\n"
            "赤や黄色の 色さまざまに\n水の上にも 織る錦\n"
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
        "additional_verses": [
            kana_notes(
                "しゃ ぼ ん だ ま き え た "
                "と ば ず に き え た "
                "う ま れ て す ぐ に "
                "こ わ れ て き え た"
            ),
        ],
        # 結びの2行は1・2番より短い。後半は「しゃぼん玉飛ばそ」の8モーラを
        # 収めるため、冒頭の8分音符を16分2つへ分ける。
        "tail_score": [
            ("か", A3, E8), ("ぜ", D4, S16), ("か", D4, S16), ("ぜ", D4, E8),
            ("ふ", E4, E8), ("う", FS4, E8), ("く", A4, E8), ("な", A4, E8),
            None,
            ("しゃ", B4, S16), ("ぼ", B4, S16), ("ん", G4, E8), ("だ", D5, E8),
            ("ま", B4, E8), ("と", A4, E8), ("ば", B4, E8), ("そ", A4, E8),
        ],
        "tail_chords": ["D", "D", "G", "A7"],
        # ニ長調。前奏1小節 + 8小節(2小節×4行)
        "chords": [
            "D",
            "D", "D", "G", "A7",
            "D", "A7", ("G", "D"), ("A7", "D"),
        ],
        "lyrics": (
            "しゃぼん玉飛んだ\n屋根まで飛んだ\n屋根まで飛んで\nこわれて消えた\n"
            "しゃぼん玉消えた\n飛ばずに消えた\n生まれてすぐに\nこわれて消えた\n"
            "風 風 吹くな\nしゃぼん玉 飛ばそ\n"
        ),
    },
    "amazinggrace": {
        "title": "Amazing Grace",
        "title_kana": "アメイジンググレイス",
        "description": "賛美歌・PD",
        "tempo": 750_000,  # ♩=80
        "time": (3, 4),
        # ト長調(旋律 New Britain の慣用調)。3拍子の3拍目から歌い出す弱起で、
        # 4行とも小節の途中(次の行の弱起)で行が変わるので区切りは BR。
        # 旋律の骨格は ソ|ド ミ(ド) ミ レ ド ラ ソ の五音音階。
        # 「A-ma-zing」の「zing」など、楽譜で2音に渡る音節は2音符に割る。
        "score": [
            H,  # 弱起(3拍目から)
            ("ア", D4, Q),
            # ---- Amazing grace, how sweet the sound ----
            ("メ", G4, E8), ("イ", G4, DQ),
            ("ジ", B4, E8), ("ン", G4, S16), ("グ", G4, S16),
            ("グ", B4, S16), ("レ", B4, S16), ("イ", B4, Q), ("ス", B4, E8),
            ("ハ", A4, E8), ("ウ", A4, E8),
            ("ス", G4, S16), ("イ", G4, Q), ("ー", G4, E8), ("ト", G4, S16),
            ("ザ", E4, Q),
            ("サ", D4, E8), ("ウ", D4, Q), ("ン", D4, S16), ("ド", D4, S16),
            BR,
            # ---- That saved a wretch like me ----
            ("ザ", D4, E8), ("ッ", D4, S16), ("ト", D4, S16),
            ("セ", G4, E8), ("イ", G4, Q), ("ブ", G4, S16), ("ド", G4, S16),
            ("ア", B4, E8), ("ア", G4, E8),  # 「a」は楽譜どおり2音のメリスマ
            ("レ", B4, Q), ("ッ", B4, E8), ("チ", B4, E8),
            ("ラ", A4, E8), ("イ", A4, S16), ("ク", A4, S16),
            ("ミ", D5, H), ("ー", D5, H),  # 4拍のばして半終止(D7)
            Q,  # ブレス
            BR,
            # ---- I once was lost, but now am found ----
            ("ア", B4, E8), ("イ", B4, E8),
            ("ワ", D5, Q), ("ン", D5, E8), ("ス", D5, E8),
            ("ワ", B4, E8), ("ズ", G4, E8),  # 「was」は楽譜どおり2音のメリスマ
            ("ロ", B4, Q), ("ス", B4, E8), ("ト", B4, E8),
            ("バ", A4, E8), ("ッ", A4, S16), ("ト", A4, S16),
            ("ナ", G4, Q), ("ウ", G4, Q),
            ("ア", E4, E8), ("ム", E4, E8),
            ("フ", D4, S16), ("ア", D4, DE), ("ウ", D4, E8), ("ン", D4, S16), ("ド", D4, S16),
            BR,
            # ---- Was blind, but now I see ----
            ("ワ", D4, E8), ("ズ", D4, E8),
            ("ブ", G4, S16), ("ラ", G4, DE), ("イ", G4, E8), ("ン", G4, S16), ("ド", G4, S16),
            ("バ", B4, E8), ("ッ", G4, S16), ("ト", G4, S16),  # 2音めがメリスマ
            ("ナ", B4, Q), ("ウ", B4, Q),
            ("ア", A4, E8), ("イ", A4, E8),
            ("シ", G4, H), ("ー", G4, DH),
        ],
        # ト長調。前奏1小節 + 弱起の小節 + 16小節(最後の小節は3拍で終わる)。
        # 賛美歌の定番進行 I - IV - I - V7 - I(「sweet the」でIV、「like me」でV7)
        "chords": [
            "G",
            "G",
            "G", "G", "C", "G",
            "G", ("G", "G", "D7"), "D7", ("D7", "D7", "G"),
            "G", "G", "C", "G",
            "G", ("G", "G", "D7"), "G", "G",
        ],
        "lyrics": (
            "Amazing grace! How sweet the sound\n"
            "That saved a wretch like me!\n"
            "I once was lost, but now am found,\n"
            "Was blind, but now I see.\n"
        ),
        "xf_words": [
            [("Amazing", 6), ("grace!", 4), ("How", 2), ("sweet", 4),
             ("the", 1), ("sound", 4)],
            [("That", 3), ("saved", 4), ("a", 2), ("wretch", 3),
             ("like", 3), ("me!", 2)],
            [("I", 2), ("once", 3), ("was", 2), ("lost,", 3), ("but", 3),
             ("now", 2), ("am", 2), ("found,", 5)],
            [("Was", 2), ("blind,", 5), ("but", 3), ("now", 2), ("I", 2),
             ("see.", 2)],
        ],
    },
    "twinkle": {
        "title": "Twinkle Twinkle Little Star",
        "title_kana": "ツインクルリトルスター",
        "description": "英語童謡・PD",
        "tempo": 600_000,  # ♩=100
        "time": (4, 4),
        # ハ長調(原曲 "Ah! vous dirai-je, maman" の慣用調)。1行=2小節で
        # ド ド ソ ソ ラ ラ ソー / ファ ファ ミ ミ レ レ ドー の素直な進行。
        # 英語の1音節を複数モーラに割る都合で8分・16分に分割してある。
        "score": [
            # ---- Twinkle twinkle little star ----
            ("ツ", C4, E8), ("イ", C4, S16), ("ン", C4, S16),
            ("ク", C4, E8), ("ル", C4, E8),
            ("ツ", G4, E8), ("イ", G4, S16), ("ン", G4, S16),
            ("ク", G4, E8), ("ル", G4, E8),
            ("リ", A4, Q),
            ("ト", A4, E8), ("ル", A4, E8),
            ("ス", G4, S16), ("タ", G4, DE), ("ー", G4, Q),
            None,
            # ---- How I wonder what you are ----
            ("ハ", F4, E8), ("ウ", F4, E8),
            ("ア", F4, E8), ("イ", F4, E8),
            ("ワ", E4, E8), ("ン", E4, E8),
            ("ダ", E4, E8), ("ー", E4, E8),
            ("ワ", D4, E8), ("ッ", D4, S16), ("ト", D4, S16),
            ("ユ", D4, E8), ("ー", D4, E8),
            ("ア", C4, Q), ("ー", C4, Q),
            None,
            # ---- Up above the world so high ----
            ("ア", G4, E8), ("ッ", G4, S16), ("プ", G4, S16),
            ("ア", G4, Q),
            ("バ", F4, E8), ("ブ", F4, E8),
            ("ザ", F4, Q),
            ("ワ", E4, S16), ("ー", E4, S16), ("ル", E4, S16), ("ド", E4, S16),
            ("ソ", E4, E8), ("ー", E4, E8),
            ("ハ", D4, Q), ("イ", D4, Q),
            None,
            # ---- Like a diamond in the sky ----
            ("ラ", G4, E8), ("イ", G4, S16), ("ク", G4, S16),
            ("ア", G4, Q),
            ("ダ", F4, E8), ("イ", F4, S16), ("ヤ", F4, S16),
            ("モ", F4, E8), ("ン", F4, S16), ("ド", F4, S16),
            ("イ", E4, E8), ("ン", E4, E8),
            ("ザ", E4, Q),
            ("ス", D4, S16), ("カ", D4, DE), ("イ", D4, Q),
            None,
            # ---- Twinkle twinkle little star(繰り返し) ----
            ("ツ", C4, E8), ("イ", C4, S16), ("ン", C4, S16),
            ("ク", C4, E8), ("ル", C4, E8),
            ("ツ", G4, E8), ("イ", G4, S16), ("ン", G4, S16),
            ("ク", G4, E8), ("ル", G4, E8),
            ("リ", A4, Q),
            ("ト", A4, E8), ("ル", A4, E8),
            ("ス", G4, S16), ("タ", G4, DE), ("ー", G4, Q),
            None,
            # ---- How I wonder what you are(繰り返し) ----
            ("ハ", F4, E8), ("ウ", F4, E8),
            ("ア", F4, E8), ("イ", F4, E8),
            ("ワ", E4, E8), ("ン", E4, E8),
            ("ダ", E4, E8), ("ー", E4, E8),
            ("ワ", D4, E8), ("ッ", D4, S16), ("ト", D4, S16),
            ("ユ", D4, E8), ("ー", D4, E8),
            ("ア", C4, Q), ("ー", C4, Q),
        ],
        # ハ長調。前奏1小節 + 12小節(2小節×6行)。定番の I - IV - V7 進行
        "chords": [
            "C",
            "C", ("F", "C"), ("F", "C"), ("G7", "C"),
            ("C", "F"), ("C", "G7"), ("C", "F"), ("C", "G7"),
            "C", ("F", "C"), ("F", "C"), ("G7", "C"),
        ],
        "lyrics": (
            "Twinkle, twinkle, little star,\n"
            "How I wonder what you are!\n"
            "Up above the world so high,\n"
            "Like a diamond in the sky.\n"
            "Twinkle, twinkle, little star,\n"
            "How I wonder what you are!\n"
        ),
        "xf_words": [
            [("Twinkle,", 5), ("twinkle,", 5), ("little", 3), ("star,", 3)],
            [("How", 2), ("I", 2), ("wonder", 4), ("what", 3), ("you", 2),
             ("are!", 2)],
            [("Up", 3), ("above", 3), ("the", 1), ("world", 4), ("so", 2),
             ("high,", 2)],
            [("Like", 3), ("a", 1), ("diamond", 6), ("in", 2), ("the", 1),
             ("sky.", 3)],
            [("Twinkle,", 5), ("twinkle,", 5), ("little", 3), ("star,", 3)],
            [("How", 2), ("I", 2), ("wonder", 4), ("what", 3), ("you", 2),
             ("are!", 2)],
        ],
    },
}


def chord_pitches(symbol: str) -> tuple[int, list[int]]:
    """コード名 → (ベース音, ブロックコードの構成音)。

    ベースは BASS_LOW から1オクターブ、コードはルートを CHORD_LOW から
    1オクターブの帯域に置く(メロディと帯域が重ならないようにするため)。

    第7音だけは、素直に積むとコード帯域の上へ大きくはみ出してメロディの上に
    飛び出すこと(G7 なら G3-B3-D4-F4 で最高音が F4)があるので、はみ出す場合は
    1オクターブ下げてルートの下に置く。第7音は次の和音へ半音で下降解決する音
    なので、下に置いたほうが声部進行としても素直になる(G7→C なら F3→E3)。
    """
    m = _CHORD_RE.fullmatch(symbol)
    if m is None:
        raise ValueError(f"未対応のコード名: {symbol}")
    letter, accidental, quality = m.groups()
    pc = (_ROOTS[letter] + {"": 0, "#": 1, "b": -1}[accidental]) % 12
    root = CHORD_LOW + (pc - CHORD_LOW) % 12
    bass = BASS_LOW + (pc - BASS_LOW) % 12
    tones = [root + i for i in _QUALITIES[quality or ""]]
    if len(tones) == 4 and tones[3] >= CHORD_LOW + 12:
        tones[3] -= 12
    return bass, sorted(tones)


def accompaniment_events(song: dict, measures: int) -> list[tuple[int, int, int, int]]:
    """コード進行から伴奏の (start, dur, note, velocity) を作る。

    拍子の分子に応じた刻み(BEAT_PATTERNS)で、拍頭にベース音とブロックコードを
    交互に置くだけの単純な伴奏。凝った動きはしない。
    """
    num, den = song["time"]
    beat = TPB * 4 // den
    pattern = BEAT_PATTERNS[num]
    chords = song["chords"]
    if len(chords) != measures:
        raise ValueError(
            f"{song['title']}: chords が {len(chords)} 小節ぶん(score は {measures} 小節)"
        )
    dur = int(beat * ACC_GATE)
    events: list[tuple[int, int, int, int]] = []
    for index, entry in enumerate(chords):
        symbols = (entry,) if isinstance(entry, str) else tuple(entry)
        if num % len(symbols):
            raise ValueError(f"{song['title']}: {num}/{den} を {len(symbols)} 等分できません")
        for b, role in enumerate(pattern):
            symbol = symbols[b * len(symbols) // num]
            bass, tones = chord_pitches(symbol)
            start = (index * num + b) * beat
            if role == "chord":
                events += [(start, dur, n, CHORD_VELOCITY) for n in tones]
                continue
            # 5度のベースは「同じ和音が続いている間の刻み」。その拍で和音が
            # 変わるならルートを鳴らす(変えないと、たとえば4/4の3拍目から
            # 主和音に解決する終止で最後までベースが5度のままになり、
            # ドミナントからバスが動かない第2転回の終止になってしまう)
            if role == "fifth" and symbol == symbols[0]:
                note = bass + 7
                if note >= BASS_LOW + 12:
                    note -= 12
            else:
                note = bass
            events.append((start, dur, note, BASS_VELOCITY))
    return events


def _append_notes(track: MidiTrack, notes: list[tuple[int, int, int, int, int]]) -> None:
    """(start, dur, note, velocity, channel) の列を絶対tick順にトラックへ書く。"""
    events: list[tuple[int, int, Message]] = []
    for start, dur, note, velocity, channel in notes:
        events.append(
            (start, 1, Message("note_on", channel=channel, note=note, velocity=velocity, time=0))
        )
        events.append(
            (start + dur, 0, Message("note_off", channel=channel, note=note, velocity=64, time=0))
        )
    events.sort(key=lambda e: (e[0], e[1]))  # 同tickでは note_off を先に
    prev = 0
    for t, _, msg in events:
        msg.time = t - prev
        track.append(msg)
        prev = t
    track.append(MetaMessage("end_of_track", time=0))


def _track_chunk_bytes(mid: MidiFile) -> bytes:
    buf = io.BytesIO()
    mid.save(file=buf)
    data = buf.getvalue()
    start = data.index(b"MTrk")
    return data[start:]


def expanded_score(song: dict) -> list:
    """1番の譜面へ、同じ旋律で歌う2番以降のカナを載せて展開する。"""
    base = song["score"]
    additional = song.get("additional_verses", ())
    note_count = sum(isinstance(item, tuple) for item in base)
    out = list(base)
    for verse_no, kana in enumerate(additional, start=2):
        if len(kana) != note_count:
            raise ValueError(
                f"{song['title']} {verse_no}番: 歌唱カナが{len(kana)}個"
                f"（旋律は{note_count}音）"
            )
        # 番の境目は次の小節頭へ進める。弱起の曲は base 冒頭の休符も
        # そのまま再生されるため、1番と同じ位置から歌い出せる。
        out.append(None)
        index = 0
        for item in base:
            if isinstance(item, tuple):
                _, note, dur = item
                out.append((kana[index], note, dur))
                index += 1
            else:
                out.append(item)
    if tail := song.get("tail_score"):
        out.extend([None, *tail])
    return out


def expanded_chords(song: dict) -> list:
    """前奏は1回だけ残し、歌の伴奏小節を番数ぶん繰り返す。"""
    chords = song["chords"]
    verse_count = 1 + len(song.get("additional_verses", ()))
    return [chords[0], *(chords[1:] * verse_count), *song.get("tail_chords", ())]


def xf_lyric_fragments(song: dict, score: list) -> list[str] | None:
    """英語表記とscoreのカナをXFの ``表記[読み]`` 断片へ変換する。"""
    word_lines = song.get("xf_words")
    if word_lines is None:
        return None

    words = [word for line in word_lines for word in line]
    note_count = sum(isinstance(item, tuple) for item in score)
    mora_count = sum(count for _, count in words)
    if mora_count != note_count:
        raise ValueError(
            f"{song['title']}: xf_words は{mora_count}モーラ"
            f"（score は{note_count}音）"
        )

    fragments: list[str] = []
    for line in word_lines:
        for word_index, (surface, count) in enumerate(line):
            display = surface + (" " if word_index < len(line) - 1 else "")
            for mora_index in range(count):
                prefix = f"{display}[" if mora_index == 0 else ""
                suffix = "]" if mora_index == count - 1 else ""
                fragments.append(prefix + "{kana}" + suffix)
    return fragments


def build(song: dict, *, full: bool = True) -> bytes:
    num, den = song["time"]
    meas = TPB * num * 4 // den
    lead_in = meas  # 1小節ぶんの前奏(無音)
    notes: list[tuple[int, int, int]] = []  # (start, dur, note)
    lyric_events: list[tuple[int, str]] = []  # (tick, text)
    tick = lead_in
    line_break = False
    score = expanded_score(song) if full else list(song["score"])
    fragments = xf_lyric_fragments(song, score)
    fragment_iter = iter(fragments or ())
    for item in score:
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
        fragment = next(fragment_iter).format(kana=kana) if fragments is not None else kana
        lyric_events.append((tick, ("/" if line_break else "") + fragment))
        line_break = False
        tick += dur

    mid = MidiFile(ticks_per_beat=TPB, charset="cp932")
    track = MidiTrack()
    mid.tracks.append(track)
    track.append(MetaMessage("set_tempo", tempo=song["tempo"], time=0))
    track.append(MetaMessage("time_signature", numerator=num, denominator=den, time=0))
    _append_notes(track, [(s, d, n, 100, MELODY_CHANNEL) for s, d, n in notes])

    # 伴奏は別トラック・別チャンネル(mix.py がメロディchのnoteだけ消すので残る)
    measures = -(-tick // meas)  # メロディが収まる小節数(切り上げ)
    acc_track = MidiTrack()
    mid.tracks.append(acc_track)
    acc_track.append(Message("program_change", channel=ACC_CHANNEL, program=ACC_PROGRAM, time=0))
    expanded = {**song, "chords": expanded_chords(song) if full else song["chords"]}
    _append_notes(
        acc_track,
        [(s, d, n, v, ACC_CHANNEL) for s, d, n, v in accompaniment_events(expanded, measures)],
    )

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


def first_verse_lyrics(song: dict) -> str:
    """基本譜面（追加番・結びを除く）に対応する先頭の歌詞だけ返す。"""
    line_count = 1 + sum(item is None or item == BR for item in song["score"])
    lines = song["lyrics"].splitlines()
    if len(lines) < line_count:
        raise ValueError(f"{song['title']}: 1番の譜面は{line_count}行（歌詞は{len(lines)}行）")
    return "\n".join(lines[:line_count]) + "\n"


def generated_samples() -> list[dict]:
    """通常版とフル版を、manifestへ並べる順序で展開する。"""
    samples = []
    for sid, song in SONGS.items():
        has_full_variant = bool(song.get("additional_verses") or song.get("tail_score"))
        samples.append(
            {
                "id": sid,
                "song": song,
                "full": not has_full_variant,
                "title": song["title"],
                "lyrics": first_verse_lyrics(song) if has_full_variant else song["lyrics"],
            }
        )
        if has_full_variant:
            samples.append(
                {
                    "id": f"{sid}_full",
                    "song": song,
                    "full": True,
                    "title": f"{song['title']}（フル）",
                    "lyrics": song["lyrics"],
                }
            )
    return samples


if __name__ == "__main__":
    out_dir = Path(__file__).parent.parent / "src" / "soramimic_video" / "static" / "sample"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for sample in generated_samples():
        sid = sample["id"]
        song = sample["song"]
        data = build(song, full=sample["full"])
        (out_dir / f"{sid}.mid").write_bytes(data)
        (out_dir / f"{sid}_lyrics.txt").write_text(sample["lyrics"], encoding="utf-8")
        manifest.append(
            {
                "id": sid,
                "title": sample["title"],
                "title_kana": song["title_kana"],
                "description": song["description"],
            }
        )
        print(f"wrote {sid}.mid ({len(data)} bytes)")
    (out_dir / "samples.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote samples.json ({len(manifest)} songs)")
