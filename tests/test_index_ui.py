"""トップ画面(static/index.html)の構造テスト。

ブラウザを起動せずに守れる約束だけを見る。
- 詳細設定の「読まなくても操作できる説明」は data-info で ⓘ に畳まれている
- 状態・警告・エラーの動的メッセージは畳まれていない(常時表示のまま)
- エディタへの導線はカードの⚙ボタン1つで、保存済みの選択UIがある
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "src/soramimic_video/static/index.html"


class _Collector(HTMLParser):
    """開始タグを (tag, attrs dict) の列で集める。"""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _tags() -> list[tuple[str, dict[str, str | None]]]:
    p = _Collector()
    p.feed(INDEX.read_text(encoding="utf-8"))
    return p.tags


def _markup() -> str:
    """<script> より前(=画面のマークアップ)だけを返す。"""
    text = INDEX.read_text(encoding="utf-8")
    return text[: text.index("<script>")]


def test_info_hints_cover_the_static_explanations():
    ids = {a.get("id") for tag, a in _tags() if "data-info" in a}
    # 各グループの1行説明(opt-group-lead)はすべて ⓘ の中
    leads = [a for tag, a in _tags() if "opt-group-lead" in (a.get("class") or "")]
    assert len(leads) == 3
    assert all("data-info" in a for a in leads)
    # 動的メッセージ(状態・警告・エラー)は畳まない
    always_visible = {
        "midi-restore-hint",    # 前回のファイルを復元: ...
        "layout-file-msg",      # 読み込みました: xxx.json / 読めませんでした
        "midi-error",           # 歌詞なしMIDIの拒否
        "lyrics-midi-warn",     # 元歌詞とMIDI歌詞の食い違い
        "sample-status",        # ✓ ...をセットしました
        "editor-auto-status",   # エディタの編集内容が使われます/使いません
        "parody-status",        # 編集済みの替え歌を使用します
        "voicevox-credit",
        "auto-octave-hint",
        "le-cue-hint",
        "editor-resume-note",
    }
    assert not (ids & always_visible)


def test_static_hints_in_advanced_are_all_folded():
    """詳細設定の中に、ⓘ にもidにも属さない裸の説明文を残さない。"""
    markup = _markup()
    body = markup[markup.index('<details class="card" id="advanced">') :]
    body = body[: body.index("</details>\n\n<details class=\"card\" id=\"history\"")]
    for m in re.finditer(r"<p class=\"hint[^\"]*\"([^>]*)>", body):
        attrs = m.group(1)
        assert "data-info" in attrs or "id=" in attrs, m.group(0)


def test_editor_entry_point_is_a_single_button():
    markup = _markup()
    assert "editorで変換・編集" not in markup
    # 詳細設定にあった「✏️ 替え歌を編集」はカードの⚙に置き換えた
    assert "✏️ 替え歌を編集" not in markup
    assert 'id="open-editor"' not in markup
    ids = [a.get("id") for tag, a in _tags() if tag == "button" and a.get("id")]
    # エディタを開く導線はこれ1つ(モーダル側の閉じる/取り込みは別物)
    assert ids.count("builder-edit") == 1
    # 保存済みがあるときの2択(+やめる)
    for btn in ("editor-resume-continue", "editor-resume-regen", "editor-resume-cancel"):
        assert btn in ids


def test_editor_resume_panel_is_hidden_by_default():
    panel = next(a for tag, a in _tags() if a.get("id") == "editor-resume")
    assert "hidden" in panel
    assert panel.get("role") == "group"
    assert panel.get("aria-labelledby") == "editor-resume-title"


def _script() -> str:
    """<script> 以降(=画面のふるまい)だけを返す。"""
    text = INDEX.read_text(encoding="utf-8")
    return text[text.index("<script>") :]


def _function_body(script: str, head: str) -> str:
    """head で始まる関数の中身(次の行頭 } まで)を返す。"""
    body = script[script.index(head) :]
    return body[: body.index("\n}\n")]


def test_submit_takes_the_midi_from_the_current_song_choice():
    """投入は「いま選ばれている曲」のMIDIで行う(古い・別の曲のMIDIで生成しない)。"""
    script = _script()
    submit = _function_body(script, "async function submitJob(")
    # 曲の突き合わせ・取り直しは FormData を組み立てるより前に済ませる
    assert (submit.index("ensureSelectedSampleMidi()")
            < submit.index('form.append("midi"'))
    guard = _function_body(script, "async function ensureSelectedSampleMidi(")
    # 来歴(midiSampleId)が選択と揃わなければ投入させない
    assert 'midiSampleId === $("sample-select").value' in guard
    assert "return false;" in guard
    # 曲名も同じ来歴から決める(MIDIは別の曲・曲名は選んだ曲、を作らない)
    assert "midiSampleId" in _function_body(script, "function songTitleOf(file)")


def test_editor_opens_from_the_setup_screen():
    """⚙はサーバーに変換させず(convert=0)、セットアップ画面から開く。

    いま選ばれている単語リスト・絞り込み・変換パラメータ・曲名は初期値として
    送る(エディタのセットアップ画面がそれを初期選択にする)。単語リストが
    空でも呼べる——リストはセットアップ画面で選べるため。
    """
    script = _script()
    body = _function_body(script, "async function convertAndOpenEditor()")
    assert 'form.append("convert", "0")' in body
    assert 'form.append("wordlist", $("wordlist").value.trim())' in body
    assert 'form.append("where", $("where").value.trim())' in body
    # 変換パラメータも初期値として送る(エディタの初期表示と生成条件を揃える)
    assert 'form.append("convert_params", buildConvertParams())' in body
    # セットアップ画面に出す曲名(投入時と同じ来歴から決める)
    assert 'form.append("song_title", songTitleOf(midi))' in body
    # 単語リスト名が無いことを理由に止める門番はもう無い
    assert "「続きから再開」で開き" not in body


def test_regenerate_button_says_it_starts_from_the_setup_screen():
    """「再生成」は実態がセットアップ画面からのやり直しなので、そう名乗る。"""
    markup = _markup()
    assert "設定から作り直す" in markup
    assert "現在のパラメータで再生成" not in markup
    # 説明文も揃える(押すとどこから始まるかが分かること)
    script = _script()
    note = _function_body(script, "async function openEditorFlow()")
    assert "「設定から作り直す」" in note


def test_setup_seed_has_no_results_so_viewing_alone_is_not_an_edit():
    """未変換シードは results 等を持たないので、来歴ガードは「編集なし」と見る。

    セットアップ画面で変換されると results が入って指紋が変わり、
    その結果が(取り込み操作なしで)そのまま生成に使われる。
    """
    script = _script()
    sig = _function_body(script, "function editorContentSig(data)")
    assert "data.results" in sig and "data.tokensList" in sig and "data.unitsList" in sig
    live = _function_body(script, "function liveEditorEdit()")
    assert "sig === meta.sig" in live and 'state: "none"' in live


def test_restored_sample_midi_is_refetched_at_startup():
    """復元したMIDIがサンプル曲なら、保存時点の中身を使わず取り直す。"""
    init = _function_body(_script(), "async function initBuilder()")
    assert "applySample({ midiOnly: true })" in init


def test_sample_midi_fetch_bypasses_the_browser_cache():
    """同梱サンプルは作り直されるので、キャッシュ済みの古い版を使わない。"""
    apply_sample = _function_body(_script(), "async function applySample(")
    assert 'cache: "no-store"' in apply_sample


def test_info_toggle_sets_aria_state():
    """ⓘ ボタンは aria-expanded / aria-controls を持つ(setupInfoHints)。"""
    script = INDEX.read_text(encoding="utf-8")
    for needle in ('setAttribute("aria-expanded"', 'setAttribute("aria-controls"',
                   'setAttribute("aria-label"'):
        assert needle in script


def test_info_hint_opens_as_floating_popover():
    """説明はその場に展開せず、ⓘ の近くに重ねるフロート表示にする。

    その場に開くと周りのフォームが押し下がるので、position:fixed +
    JS の座標計算(placeInfoPop)にしている。閉じる導線は ⓘ 再タップ・
    外側タップ・Esc の3つ。
    """
    text = INDEX.read_text(encoding="utf-8")
    rule = text[text.index(".hint[data-info] {"):]
    rule = rule[: rule.index("}")]
    assert "position: fixed" in rule
    assert "max-width: min(" in rule           # スマホで画面幅を超えない
    for needle in ("function placeInfoPop", "function openInfoPop", "function closeInfoPop",
                   'ev.key !== "Escape"'):
        assert needle in text


def test_editor_wordlist_is_written_back_to_the_form():
    """エディタの⚙で単語リストを変えたら、親の正本(#wordlist / #where)へ書き戻す。

    書き戻さないと、カードのプルダウン・サムネプレビュー・生成時の単語画像/
    レイアウトの解決が古いリストのままになる(どれも正本を見ている)。
    """
    script = _script()
    body = _function_body(script, "function applyEditorWordlist()")
    # 名前付きリスト(filepath) → stem を既存の選択経路へ流し、where はエディタ優先
    assert r'String(w.filepath || "").replace(/.*\//, "").replace(/\.csv$/, "")' in body
    assert "selectWordlist(name);" in body
    assert "if (wantWhere) setWhere(wantWhere);" in body
    # 名前で引けないリスト(ORIGINAL / 古い custom:<sid>)は名前も絞り込みも空にする
    assert 'const own = value === "ORIGINAL" || value.startsWith("custom:");' in body
    assert 'if (!sel.hidden) sel.selectedIndex = -1;' in body
    assert '$("wordlist").value = "";' in body
    assert 'setWhere("");' in body
    # 書き戻しで来歴が食い違って編集が捨てられないよう、シードの来歴も付け替える
    assert "adoptEditorSeedProvenance();" in body
    # 変わっていなければ何もしない(毎周期で来歴を付け替えると曲の差し替えを
    # 見逃してしまう)
    assert "return;   // すでに一致している" in body
    # 拾うのは監視ポーリングと「閉じる」の2経路
    assert "if (on) editorWatchTimer = setInterval(syncEditorSession, 1500);" in script
    close = _function_body(script, "function closeEditor()")
    assert "syncEditorSession();" in close


def test_card_selects_mirror_the_canonical_form():
    """カードのプルダウンは正本の写し。選択肢も値も片方向に写して二重管理しない。"""
    script = _script()
    opts = _function_body(script, "function syncBuilderOptions()")
    # 選択肢は optgroup ごとそのまま複製する(表示名の付け直しをしない)
    assert '$("builder-sample").innerHTML = $("sample-select").innerHTML;' in opts
    assert '$("builder-wordlist").innerHTML = wl.innerHTML;' in opts
    # 単語リストのセレクトが出ない構成(editor conf 無し)ではカード側も出さない
    assert '$("builder-wordlist-field").hidden = wl.hidden;' in opts
    assert "syncBuilderValues();" in opts
    # カード → 正本 → change の順(既存の applySample / applyWordlistSelection を通す)
    wiring = script[script.index('$("builder-sample").addEventListener'):]
    wiring = wiring[: wiring.index('$("sample-select").addEventListener')]
    assert '$("sample-select").value = $("builder-sample").value;' in wiring
    assert 'sel.dispatchEvent(new Event("change", { bubbles: true }));' in wiring


def test_card_wordlist_select_shows_the_editor_own_list():
    """エディタの中で自作リストを使っているあいだは、合成の選択肢で選択済みに見せる。

    自作リスト(ORIGINAL/csvText)のときは正本 #wordlist が空になるので、その
    ままだとカードのプルダウンが「未選択」になって何に空耳させているか分からない。
    """
    script = _script()
    assert 'const EDITOR_WORDLIST_VALUE = "__editor__";' in script
    assert 'const EDITOR_WORDLIST_LABEL = "自作リスト(替え歌エディタ)";' in script
    # 正本に名前が入っていればそちらが優先(古い ORIGINAL セッションに引きずられない)
    shows = _function_body(script, "function showsEditorWordlist()")
    assert "return !currentWordlistName() && usesEditorWordlist();" in shows
    body = _function_body(script, "function syncBuilderValues()")
    assert "const own = showsEditorWordlist();" in body
    assert "card.appendChild(o);" in body     # 自作リストのあいだだけ足す
    assert "synth.remove();" in body          # 名前付きリストに戻ったら取り除く
    assert 'card.value = own ? EDITOR_WORDLIST_VALUE : (wl.hidden ? "" : wl.value);' in body
    # 選び直されても正本は触らない(「何も選ばない」に落とさない)
    assert "if (v === EDITOR_WORDLIST_VALUE) { syncBuilderValues(); return; }" in script
# ---- エディタからの「曲を変えたい」依頼(hostRequest)にホストが応える ----


def test_editor_seed_advertises_the_song_choices():
    """シードに host.songs / host.canUploadSong / song.id を載せる。

    エディタ側は MIDI の実体も解析も持たないので、選べる曲は親が教える。
    一覧は正本(#sample-select)の写しで、カードの曲プルダウンと同じもの。
    """
    script = _script()
    body = _function_body(script, "function withHostInfo(payload)")
    assert "data.host = { songs: hostSongList(), canUploadSong: true };" in body
    # 曲のIDは来歴(midiSampleId)から決める。自分で選んだMIDIには付けない
    assert "if (midiSampleId) song.id = midiSampleId;" in body
    assert "else delete song.id;" in body
    songs = _function_body(script, "function hostSongList()")
    assert '[...$("sample-select").options]' in songs
    assert "id: o.value, title: o.textContent" in songs
    # セットアップ画面のシードはこれを通してから書く
    opened = _function_body(script, "async function convertAndOpenEditor()")
    assert "const seed = JSON.stringify(withHostInfo(body));" in opened


def test_host_request_is_polled_and_handled_once():
    """依頼の処理は監視ポーリングの中で、開いているあいだだけ、1件ずつ。"""
    script = _script()
    sync = _function_body(script, "function syncEditorSession()")
    assert "handleHostRequest();" in sync
    body = _function_body(script, "async function handleHostRequest()")
    # 多重処理を防ぐ(処理中フラグと、応えた nonce の記録)
    assert "if (hostRequestBusy) return;" in body
    assert "hostRequestSeen = key;" in body
    assert "if (key === hostRequestSeen) { clearHostRequest(key); return; }" in body
    # 閉じているあいだは応えない
    assert 'if ($("editor-frame-wrap").hidden) return;' in body
    # 成否によらず依頼は消す(残すとエディタが待ち続ける)
    assert "clearHostRequest(key);" in body and "hostRequestBusy = false;" in body
    # 応えているあいだに来た別の依頼は消さない(次の周期で応える)
    clear = _function_body(script, "function clearHostRequest(key)")
    assert "if (key && hostRequestKey(req) !== key) return;" in clear
    # 失敗は親のエディタ用メッセージ欄に出す
    assert '$("open-editor-msg").textContent = "曲を変えられませんでした: "' in body
    # 閉じたら止める(監視を切り、応えないまま残った依頼も持ち越さない)
    close = _function_body(script, "function closeEditor()")
    assert "watchEditorSession(false);" in close and "clearHostRequest();" in close
    show = _function_body(script, "function showEditorFrame()")
    assert 'hostRequestSeen = "";' in show and "hostRequestBusy = false;" in show


def test_host_song_request_moves_the_canonical_form_first():
    """曲の差し替えは正本(#sample-select / #midi)を動かしてから解析し直す。

    カードの曲プルダウン・サムネプレビュー・生成はすべて正本を見ているので、
    正本を動かさないとエディタの中だけ別の曲になってしまう。
    """
    script = _script()
    body = _function_body(script, "async function hostChangeSong(sid)")
    assert '$("sample-select").value = sid;' in body
    assert '$("sample-select").dispatchEvent(new Event("change", { bubbles: true }));' in body
    # 取得の失敗・古いMIDIの残りは既存の関門で止める
    assert "ensureSelectedSampleMidi(" in body
    assert body.index("ensureSelectedSampleMidi(") < body.index("reseedEditorSong()")
    up = _function_body(script, "async function hostUploadSong()")
    # キャンセルは何も変えずに戻る(依頼だけが消える)
    assert "if (!await pickMidiFile()) return;" in up
    # 自分のMIDIにしたらサンプルの選択は外す(投入前の突き合わせに上書きされる)
    assert '$("sample-select").value = "";' in up
    picker = _function_body(script, "function pickMidiFile()")
    for needle in ('input.addEventListener("change", onChange);',
                   'input.addEventListener("cancel", onCancel);',
                   'window.addEventListener("focus", onFocus);',
                   "input.click();"):
        assert needle in picker


def test_host_song_request_keeps_the_wordlist_and_drops_the_results():
    """曲だけを差し替える。単語リストと変換のしかたは残し、変換結果は捨てる。"""
    script = _script()
    body = _function_body(script, "async function reseedEditorSong()")
    assert 'form.append("convert", "0")' in body        # 解析のみ(既存の経路)
    for keep in ("data.phrases = fresh.phrases;", "data.host = fresh.host;",
                 "if (fresh.song) data.song = fresh.song; else delete data.song;"):
        assert keep in body
    for drop in ("delete data.results;", "delete data.tokensList;", "delete data.unitsList;"):
        assert drop in body
    # 単語リスト・パラメータには触らない(エディタ側の今の設定のまま)
    assert "data.wordlist" not in body and "data.param" not in body
    # 新しいシードの指紋と来歴を控える。付け替えないと、曲を変えた直後の編集が
    # 「別の入力から作られたもの」として捨てられてしまう
    assert "markEditorSeed(text);" in body
    # 前の曲で取り込んだ替え歌JSONは外す
    assert "clearEditorFile();" in body
