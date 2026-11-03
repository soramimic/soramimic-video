import { openDocument, openMidi, saveDocument, projectDocument, lyricMidi, lyricText, soramimicSeed } from './score-editor-io.mjs';

function simplifyUI() {
  const $ = id => document.getElementById(id);
  document.body.classList.add('local-editor');
  $('localbar').querySelector('strong').textContent = 'Soramimic SVS';
  function menu(parent, title, ids) {
    const details = document.createElement('details'), summary = document.createElement('summary');
    summary.textContent = title;
    const content = document.createElement('div'); content.className = 'local-menu';
    for (const id of ids) content.append($(id));
    details.append(summary, content); parent.append(details);
    return details;
  }
  $('localbar').insertBefore(menu($('localbar'), 'ファイル',
    ['local-open', 'local-open-audio', 'local-format', 'bsave']), $('local-name'));
  menu($('bar'), '表示・吸着', ['bsnap', 'bunit', 'bfollow', 'bbeat']);
  menu($('panel'), 'その他の編集', ['bml', 'bmr', 'bshort', 'blong', 'bpdn', 'bpup',
    'bpull', 'bpush', 'bsplit', 'bmerge', 'bdel', 'blegato', 'blegall', 'bdist', 'bquant']);
  const hidden = document.createElement('div'); hidden.hidden = true;
  for (const id of ['bistart', 'biend', 'istart', 'iend', 'bidel', 'iinfo']) hidden.append($(id));
  document.body.append(hidden);
  for (const span of $('panel').querySelectorAll('span[style]')) span.remove();
  for (const [id, text] of [['ptext', '歌詞'], ['pstart', '開始秒'], ['pend', '終了秒'], ['ppitch', '音程（MIDI番号）']]) {
    $(id).setAttribute('aria-label', text);
  }
  document.addEventListener('click', event => {
    for (const details of document.querySelectorAll('.local-editor details[open]')) {
      if (!details.contains(event.target)) details.open = false;
    }
  });
}

export async function start(editor) {
  const $ = id => document.getElementById(id);
  let current = null, filename = 'score', audioURL = null, synthURL = null, loading = false;
  let singer = null, synthesizing = false;
  let revision = 0;
  document.title = 'Soramimic SVS · 元歌を修正';
  simplifyUI();
  $('localbar').hidden = false;
  $('brebuild').disabled = true;
  $('bsynth').disabled = true;
  $('brebuild').textContent = '歌声を合成';
  $('bsynth').textContent = 'この行を合成';
  $('brebuild').title = 'PrettyPitchで端末内の歌声合成';
  $('bsynth').title = '選択した行を端末内で合成';
  $('asrc').querySelector('option[value="main"]').textContent = '元音源';
  $('asrc').querySelector('option[value="main"]').disabled = true;
  const format = $('local-format');
  const setStatus = editor.setStatus;
  const bridge = window.SoramimicBridge;
  const soramimicPeer = bridge?.peer('editor');
  $('local-soramimic').hidden = !soramimicPeer;
  const cancel = document.createElement('button'); cancel.textContent = '合成を中止'; cancel.hidden = true;
  $('brebuild').after(cancel);
  cancel.onclick = () => { ++revision; singer?.close(); };
  const credits = document.createElement('a');
  credits.href = 'https://github.com/wavtechyukky/vocatab/blob/main/voicebanks/leapsinger_ja_3singer/README.md';
  credits.target = '_blank'; credits.rel = 'noopener noreferrer';
  credits.textContent = '歌声: 波音リツ / カノン · 利用条件';
  credits.className = 'local-credits';
  $('localbar').querySelector('.local-menu').append(credits);

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
      if (!window.confirm('PrettyPitch / LeapSinger / NHVSingのモデル（約69MB）を作者の配布元から取得し、この端末内で合成します。歌詞や音源は送信しません。\n\n歌声モデルは個人・非商用向けです。公開時は「波音リツ / カノン」をクレジットしてください。ファイルメニューの「利用条件」を確認のうえ、続けますか？')) return;
      try { localStorage.setItem('svs-model-terms/1', 'accepted'); } catch { /* Consent still applies to this action. */ }
    }
    synthesizing = true; cancel.hidden = false;
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
      synthesizing = false; cancel.hidden = true;
      $('brebuild').disabled = $('bsynth').disabled = !current;
    }
  }
  $('brebuild').onclick = () => synthesize(false);
  $('bsynth').onclick = () => synthesize(true);

  async function openScore(next, name) {
    releaseAudio();
    current = next; filename = name.replace(/\.(?:json|mid|midi)$/iu, '') || 'score';
    $('local-name').textContent = name;
    $('local-help').hidden = true;
    await editor.load(current.payload);
    format.querySelector('option[value="project"]').disabled = !current.source?.song;
    $('local-open-audio').disabled = false;
    $('bsave').disabled = false;
    $('local-format').disabled = false;
    $('local-soramimic').disabled = false;
    $('brebuild').disabled = $('bsynth').disabled = synthesizing;
    $('local-soramimic').textContent = current.soramimic?.results?.length ? '替え歌の編集へ ↗' : '替え歌を作る ↗';
    if (format.selectedOptions[0]?.disabled) format.value = 'json';
  }

  $('local-soramimic').onclick = async () => {
    if (!current || !soramimicPeer) return;
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

  editor.setSave(async () => {
    if (!current) return;
    try {
      const payload = editor.read();
      let blob, extension;
      switch (format.value) {
        case 'midi': blob = new Blob([lyricMidi(current, payload.moras)], { type: 'audio/midi' }); extension = '.mid'; break;
        case 'soramimic': blob = new Blob([JSON.stringify(soramimicSeed(current, payload.moras), null, 1)], { type: 'application/json' }); extension = '.soramimic.json'; break;
        case 'lyrics': blob = new Blob([lyricText(payload.moras)], { type: 'text/plain;charset=utf-8' }); extension = '.txt'; break;
        case 'project': blob = new Blob([JSON.stringify(projectDocument(current, payload.moras), null, 1)], { type: 'application/json' }); extension = '.project.json'; break;
        default: blob = new Blob([JSON.stringify(saveDocument(current, payload), null, 1)], { type: 'application/json' }); extension = '.json';
      }
      download(blob, filename + '.edited' + extension);
      // MIDI/text are interchange exports, not a lossless save of the editor state.
      if (format.value === 'json') editor.markSaved();
      setStatus('保存しました: ' + filename + '.edited' + extension);
      if (format.value === 'soramimic') setStatus('Soramimicの「読み込み」でJSONを開き、単語リストを選んで変換してください。');
    } catch (error) { setStatus('保存できません: ' + error.message); }
  });

  $('bsave').disabled = true;
  $('local-format').disabled = true;
  $('local-open-audio').disabled = true;
  await editor.load({ moras: [], reference: [], line_texts: {}, ignore_ranges: [], has_audio: false });
  setStatus('楽譜JSON・歌詞入りMIDIを開いてください');

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
