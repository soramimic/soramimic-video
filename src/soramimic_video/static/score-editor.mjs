import { openDocument, openMidi, saveDocument, projectDocument, lyricMidi, lyricText, soramimicSeed } from './score-editor-io.mjs';

function simplifyUI(editor) {
  const $ = id => document.getElementById(id);
  document.body.classList.add('local-editor');
  const make = (tag, text, className = '') => {
    const element = document.createElement(tag); element.textContent = text; element.className = className; return element;
  };
  function group(parent, className, ids) {
    const element = make('div', '', className);
    for (const id of ids) element.append($(id));
    parent.append(element); return element;
  }
  function menu(parent, title, ids) {
    const details = make('details', ''), content = make('div', '', 'local-menu');
    for (const id of ids) content.append($(id));
    details.append(make('summary', title), content); parent.append(details); return content;
  }
  function label(id, text) {
    const element = make('label', '', 'local-field');
    element.htmlFor = id; element.append(make('span', text), $(id)); return element;
  }
  const retained = document.createElement('div'); retained.hidden = true;
  for (const id of ['local-open', 'local-format', 'local-score-file', 'local-audio-file', 'local-name', 'local-open-audio']) retained.append($(id));
  document.body.append(retained);
  const brand = $('localbar').querySelector('strong'); brand.textContent = 'Soramimic SVS';
  const identity = make('div', '', 'local-identity'); identity.append(brand, make('span', '元歌の歌詞と音符を修正'));
  const files = make('div', '', 'local-file-actions');
  $('bsave').textContent = '編集を保存';
  $('localbar').replaceChildren(identity, files, $('local-soramimic'));
  // Keep file inputs in the document when rebuilding the header.
  files.append($('local-open'), $('bsave'));
  const exportMenu = menu(files, '書き出し', ['local-format']);
  const exportButton = make('button', 'この形式で書き出す'); exportButton.id = 'local-export';
  exportMenu.append(exportButton);
  $('localbar').after($('local-name')); $('local-name').hidden = true;
  const controls = make('div', '', 'local-listening');
  controls.append(label('asrc', '聴く音源'), $('bplay'), label('aspd', '再生速度'), $('brebuild'), $('local-open-audio'));
  $('bar').prepend(controls);
  group($('bar'), 'local-history', ['bundo', 'bredo']);
  $('bundo').textContent = '元に戻す'; $('bredo').textContent = 'やり直す';
  for (const spacer of $('bar').querySelectorAll('.sp')) spacer.remove();
  $('status').setAttribute('role', 'status'); $('status').setAttribute('aria-live', 'polite');
  document.body.append($('status'));
  const rollbar = make('div', '', 'local-rollbar'); rollbar.id = 'local-rollbar';
  rollbar.append(make('strong', '歌詞と音符'), make('span', '音符を選んで編集 · ドラッグで移動・長さ変更', 'local-roll-help'));
  const zoom = make('div', '', 'local-zoom');
  zoom.append(make('span', '表示'));
  for (const [text, value, title] of [['−', .8, '楽譜を縮小'], ['＋', 1.25, '楽譜を拡大'], ['全体', 0, '楽譜全体を表示']]) {
    const button = make('button', text); button.title = title; button.setAttribute('aria-label', title);
    button.onclick = () => editor.zoom?.(value); zoom.append(button);
  }
  rollbar.append(zoom);
  menu(rollbar, '表示設定', ['bsnap', 'bunit', 'bfollow', 'bbeat']);
  $('wrap').before(rollbar);
  const help = $('local-help'); help.replaceChildren(make('h1', '元歌を開いて、歌詞と音符を修正'),
    make('p', '歌詞入りMIDIや楽譜JSONを読み込めます。Videoから開いた曲は、そのまま編集できます。'));
  const open = make('button', '楽譜を開く'); open.onclick = () => $('local-open').click();
  help.append(open, make('p', '音符を直す → 歌声を作って聴く → Soramimicで替え歌を編集', 'local-flow'));
  $('wrap').append(help);
  const heading = make('div', '', 'local-inspector-heading');
  const selected = make('strong', '音符を選ぶと、ここで編集できます'); selected.id = 'local-selection-title';
  heading.append(selected, $('bprev'), $('bnext'));
  $('panel').prepend(heading);
  $('bprev').textContent = '前の音符'; $('bnext').textContent = '次の音符';
  const fields = make('div', '', 'local-note-fields');
  fields.append(label('ptext', '歌詞（読み）'));
  const pitch = make('select', ''); pitch.id = 'local-pitch'; pitch.setAttribute('aria-label', '音程');
  const names = ['ド', 'ド♯', 'レ', 'レ♯', 'ミ', 'ファ', 'ファ♯', 'ソ', 'ソ♯', 'ラ', 'ラ♯', 'シ'];
  for (let midi = 0; midi <= 127; midi++) {
    const option = make('option', names[midi % 12] + (Math.floor(midi / 12) - 1)); option.value = midi; pitch.append(option);
  }
  const pitchField = make('label', '', 'local-field'); pitchField.htmlFor = pitch.id;
  pitchField.append(make('span', '音程'), pitch); fields.append(pitchField);
  pitch.onchange = () => { $('ppitch').value = pitch.value; $('ppitch').dispatchEvent(new window.Event('change')); };
  fields.append(label('pstart', '開始（秒）'), label('pend', '終了（秒）'));
  const duration = make('output', ''); duration.id = 'local-duration'; fields.append(duration);
  $('panel').append(fields);
  const editActions = group($('panel'), 'local-note-actions', ['bpdn', 'bpup', 'bsplit', 'bmerge', 'bdel', 'bgoto', 'bsynth']);
  $('bpdn').textContent = '半音下げる'; $('bpup').textContent = '半音上げる';
  $('bgoto').textContent = 'ここから再生';
  menu(editActions, '細かい調整', ['bml', 'bmr', 'bshort', 'blong', 'bpull', 'bpush', 'blegato', 'blegall', 'bdist', 'bquant']);
  for (const [id, text] of [['bml', '前へ移動'], ['bmr', '後ろへ移動'], ['bshort', '短くする'], ['blong', '長くする']]) $(id).textContent = text;
  const hidden = make('div', ''); hidden.hidden = true;
  for (const id of ['ppitch', 'bistart', 'biend', 'istart', 'iend', 'bidel', 'iinfo']) hidden.append($(id));
  document.body.append(hidden);
  for (const span of $('panel').querySelectorAll('span[style]')) span.remove();
  $('panel').append($('pline'));
  const selection = note => {
    selected.textContent = note ? '選択した音符' : '音符を選ぶと、ここで編集できます';
    for (const id of ['ptext', 'local-pitch', 'pstart', 'pend', 'bprev', 'bnext', 'bpdn', 'bpup', 'bsplit', 'bmerge', 'bdel', 'bgoto']) $(id).disabled = !note;
    $('panel').classList.toggle('has-selection', !!note);
    $('bsynth').disabled = !note || document.body.classList.contains('synthesizing');
    if (note) { pitch.value = String(note.pitch); duration.textContent = '長さ ' + (note.end - note.start).toFixed(3) + ' 秒'; }
    else { pitch.selectedIndex = -1; for (const id of ['ptext', 'pstart', 'pend']) $(id).value = ''; duration.textContent = '長さ —'; }
  };
  editor.setSelectionHandler?.(selection); selection(null);
  document.addEventListener('click', event => {
    for (const details of document.querySelectorAll('.local-editor details[open]')) if (!details.contains(event.target)) details.open = false;
  });
  return { exportMenu };
}

export async function start(editor) {
  const $ = id => document.getElementById(id);
  let current = null, filename = 'score', audioURL = null, synthURL = null, loading = false;
  let singer = null, synthesizing = false;
  let revision = 0;
  document.title = 'Soramimic SVS · 元歌を修正';
  const { exportMenu } = simplifyUI(editor);
  $('localbar').hidden = false;
  $('brebuild').disabled = true;
  $('bsynth').disabled = true;
  $('brebuild').textContent = '修正した歌声を作る';
  $('bsynth').textContent = 'この行の歌声を作る';
  $('brebuild').title = 'PrettyPitchで端末内の歌声合成';
  $('bsynth').title = '選択した行を端末内で合成';
  $('asrc').querySelector('option[value="main"]').textContent = '元音源';
  $('asrc').querySelector('option[value="main"]').disabled = true;
  const format = $('local-format');
  const setStatus = editor.setStatus;
  const bridge = window.SoramimicBridge;
  const soramimicPeer = bridge?.peer('editor');
  $('local-soramimic').hidden = false;
  $('local-soramimic').title = soramimicPeer ? 'Soramimicで替え歌を編集' : 'Soramimicの読み込みで開くファイルを保存';
  $('local-soramimic').textContent = soramimicPeer ? '替え歌の編集へ ↗' : 'Soramimic用に保存';
  const cancel = document.createElement('button'); cancel.textContent = '合成を中止'; cancel.hidden = true;
  $('brebuild').after(cancel);
  cancel.onclick = () => { ++revision; singer?.close(); };
  const credits = document.createElement('a');
  credits.href = 'https://github.com/wavtechyukky/vocatab/blob/main/voicebanks/leapsinger_ja_3singer/README.md';
  credits.target = '_blank'; credits.rel = 'noopener noreferrer';
  credits.textContent = '歌声: 波音リツ / カノン · 利用条件';
  credits.className = 'local-credits';
  exportMenu.append(credits);

  async function synthesize(lineOnly) {
    if (!current || synthesizing) return;
    let payload = editor.read(), input = payload.moras;
    if (lineOnly) {
      const line = editor.selectedLine?.();
      if (line == null) return setStatus('音符を選んでください。');
      input = input.filter(note => note.line === line);
    }
    let accepted = false;
    try { accepted = localStorage.getItem('svs-model-terms/1') === 'accepted'; } catch { /* Private mode. */ }
    if (!accepted) {
      if (!window.confirm('PrettyPitch / LeapSinger / NHVSingのモデル（約69MB）を作者の配布元から取得し、この端末内で合成します。歌詞や音源は送信しません。\n\n歌声モデルは個人・非商用向けです。公開時は「波音リツ / カノン」をクレジットしてください。書き出しメニューの「利用条件」を確認のうえ、続けますか？')) return;
      try { localStorage.setItem('svs-model-terms/1', 'accepted'); } catch { /* Consent still applies to this action. */ }
    }
    synthesizing = true; cancel.hidden = false; document.body.classList.add('synthesizing');
    $('brebuild').disabled = $('bsynth').disabled = true;
    const request = revision, score = JSON.stringify(payload.moras);
    try {
      setStatus('合成エンジンを準備しています…');
      const { BrowserSinger } = await import('./score-engine.mjs');
      if (request !== revision) return;
      singer ??= new BrowserSinger();
      singer.onProgress = message => { if (request === revision) setStatus(message); };
      const blob = await singer.synthesize(input);
      if (request !== revision) return;
      if (score !== JSON.stringify(editor.read().moras)) return setStatus('合成中に楽譜が変わりました。もう一度合成してください。');
      if (synthURL) URL.revokeObjectURL(synthURL);
      synthURL = URL.createObjectURL(blob);
      editor.setSynthAudio(synthURL, input[0].start);
      setStatus('合成しました。「再生」で聴けます。音源の切替で元音源と比較できます。');
    } catch (error) { setStatus(error.name === 'AbortError' ? '合成を中止しました。' : '合成できません: ' + error.message); }
    finally {
      synthesizing = false; cancel.hidden = true; document.body.classList.remove('synthesizing');
      $('brebuild').disabled = !current; $('bsynth').disabled = !current || editor.selectedLine?.() == null;
    }
  }
  $('brebuild').onclick = () => synthesize(false);
  $('bsynth').onclick = () => synthesize(true);

  async function openScore(next, name) {
    releaseAudio();
    current = next; filename = name.replace(/\.(?:json|mid|midi)$/iu, '') || 'score';
    $('local-name').textContent = name;
    document.querySelector('.local-identity > span').textContent = name;
    $('local-help').hidden = true;
    await editor.load(current.payload);
    format.querySelector('option[value="project"]').disabled = !current.source?.song;
    $('local-open-audio').disabled = false;
    $('bsave').disabled = false;
    $('local-format').disabled = false;
    $('local-soramimic').disabled = false;
    $('brebuild').disabled = synthesizing; $('bsynth').disabled = synthesizing || editor.selectedLine?.() == null;
    $('local-soramimic').textContent = soramimicPeer ? '替え歌の編集へ ↗' : 'Soramimic用に保存';
    document.body.classList.add('score-loaded');
    $('local-export').disabled = false;
    if (format.selectedOptions[0]?.disabled) format.value = 'json';
  }

  $('local-soramimic').onclick = async () => {
    if (!current) return;
    if (!soramimicPeer) return saveAs('soramimic');
    $('local-soramimic').disabled = true;
    try {
      // send opens the destination synchronously inside this user gesture.
      await bridge.send(soramimicPeer, () => soramimicSeed(current, editor.read().moras), { allowSetup: true });
      setStatus('Soramimicへ渡しました。開いた画面で替え歌を編集できます。');
    } catch (error) { setStatus('Soramimicを開けません: ' + error.message); }
    finally { $('local-soramimic').disabled = false; }
  };

  function download(blob, name) {
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  function discardAllowed() {
    return !editor.isDirty() || window.confirm('未保存の変更があります。別の楽譜を開きますか？');
  }

  function releaseAudio() {
    editor.clearAudio();
    if (audioURL) URL.revokeObjectURL(audioURL);
    if (synthURL) URL.revokeObjectURL(synthURL);
    synthURL = null;
    audioURL = null;
  }

  async function openFile(file) {
    if (loading || !file || !discardAllowed()) return;
    loading = true;
    const request = ++revision;
    try {
      if (file.size > 32 * 1024 * 1024) throw new Error('楽譜ファイルは32MBまでです。');
      setStatus('楽譜を読み込んでいます…');
      const buffer = await file.arrayBuffer();
      const isMidi = new TextDecoder().decode(buffer.slice(0, 4)) === 'MThd';
      const next = isMidi ? openMidi(buffer) : openDocument(JSON.parse(new TextDecoder().decode(buffer).replace(/^\uFEFF/u, '')));
      if (request !== revision) return;
      await openScore(next, file.name);
    } catch (error) { setStatus('楽譜を開けません: ' + error.message); }
    finally { loading = false; $('local-score-file').value = ''; }
  }

  $('local-open').onclick = () => $('local-score-file').click();
  $('local-score-file').onchange = event => openFile(event.target.files[0]);
  $('local-open-audio').onclick = () => $('local-audio-file').click();
  $('local-audio-file').onchange = async event => {
    const file = event.target.files[0];
    if (!file || !current) return;
    const request = ++revision;
    try {
      setStatus('元音源を読み込んでいます…');
      const context = new AudioContext();
      let buffer;
      try { buffer = await context.decodeAudioData(await file.arrayBuffer()); }
      finally { await context.close(); }
      if (request !== revision) return;
      const rate = 100, values = [];
      const channels = Array.from({ length: buffer.numberOfChannels }, (_, index) => buffer.getChannelData(index));
      const hop = Math.max(1, Math.round(buffer.sampleRate / rate));
      for (let index = 0; index < buffer.length; index += hop) {
        let peak = 0;
        for (const channel of channels) for (let i = index; i < Math.min(index + hop, buffer.length); i++) {
          peak = Math.max(peak, Math.abs(channel[i]));
        }
        values.push(peak);
      }
      releaseAudio(); audioURL = URL.createObjectURL(file);
      editor.setLocalAudio(audioURL, { rate, v: values });
      setStatus('元音源: ' + file.name);
    } catch (error) { setStatus('元音源を開けません: ' + error.message); }
    finally { $('local-audio-file').value = ''; }
  };

  async function saveAs(kind) {
    if (!current) return;
    try {
      const payload = editor.read();
      let blob, extension;
      switch (kind) {
        case 'midi': blob = new Blob([lyricMidi(current, payload.moras)], { type: 'audio/midi' }); extension = '.mid'; break;
        case 'soramimic': blob = new Blob([JSON.stringify(soramimicSeed(current, payload.moras), null, 1)], { type: 'application/json' }); extension = '.soramimic.json'; break;
        case 'lyrics': blob = new Blob([lyricText(payload.moras)], { type: 'text/plain;charset=utf-8' }); extension = '.txt'; break;
        case 'project': blob = new Blob([JSON.stringify(projectDocument(current, payload.moras), null, 1)], { type: 'application/json' }); extension = '.project.json'; break;
        default: blob = new Blob([JSON.stringify(saveDocument(current, payload), null, 1)], { type: 'application/json' }); extension = '.json';
      }
      download(blob, filename + '.edited' + extension);
      // MIDI/text are interchange exports, not a lossless save of the editor state.
      if (kind === 'json') editor.markSaved();
      setStatus('保存しました: ' + filename + '.edited' + extension);
      if (kind === 'soramimic') setStatus('Soramimicの「読み込み」でJSONを開き、単語リストを選んで変換してください。');
    } catch (error) { setStatus('保存できません: ' + error.message); }
  }
  editor.setSave(() => saveAs('json'));
  $('local-export').onclick = () => saveAs(format.value);
  $('local-export').disabled = true;

  $('bsave').disabled = true;
  $('local-format').disabled = true;
  $('local-open-audio').disabled = true;
  await editor.load({ moras: [], reference: [], line_texts: {}, ignore_ranges: [], has_audio: false });
  setStatus('楽譜を開くと、歌詞と音符を編集できます。');

  if (new URL(location.href).searchParams.has('lyrics-transfer')) {
    try {
      await bridge.receive(soramimicPeer, async payload => {
        if (!payload.scoreContext) throw new Error('元歌の楽譜がありません。');
        const score = payload.scoreContext;
        const next = openDocument({ format: 'soramimic-score-editor/1', source: null, song: score.song,
          payload: { moras: score.moras, line_texts: score.lineTexts }, soramimic: payload });
        await openScore(next, '元歌の楽譜');
        setStatus('元歌を修正して「替え歌の編集へ」でSoramimicに戻れます。');
      }, { allowSetup: true });
    } catch (error) { setStatus('楽譜を受け取れません: ' + error.message); }
    return;
  }

  // The source export is requested only when the user explicitly opens this
  // editor from a completed Video job. No upload or synthesis request is made.
  const job = new URL(location.href).searchParams.get('job');
  if (job && /^[a-zA-Z0-9_-]{1,80}$/u.test(job)) {
    try {
      const key = localStorage.getItem('apiKey');
      const response = await fetch('/api/jobs/' + encodeURIComponent(job) + '/exports/midi', {
        headers: key ? { 'X-API-Key': key } : {}, credentials: 'same-origin', cache: 'no-store',
        signal: AbortSignal.timeout(20000),
      });
      if (!response.ok) throw new Error('曲の保存期限またはログイン状態を確認してください。');
      const file = new File([await response.blob()], 'song.mid', { type: 'audio/midi' });
      await openFile(file);
    } catch (error) { setStatus('曲を開けません: ' + error.message); }
  }
}
