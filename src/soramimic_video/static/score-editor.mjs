import { openDocument, openMidi, saveDocument, projectDocument, lyricMidi, lyricText } from './score-editor-io.mjs';

export async function start(editor) {
  const $ = id => document.getElementById(id);
  let current = null, filename = 'score', audioURL = null, loading = false;
  let revision = 0;
  document.title = 'ソラミミック · 音符を編集';
  $('localbar').hidden = false;
  $('brebuild').disabled = true;
  $('bsynth').disabled = true;
  $('brebuild').title = $('bsynth').title = 'PrettyPitchの準備が必要です';
  $('asrc').querySelector('option[value="main"]').textContent = '元音源';
  const format = $('local-format');
  const setStatus = editor.setStatus;

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
      releaseAudio();
      current = next; filename = file.name.replace(/\.(?:json|mid|midi)$/iu, '') || 'score';
      $('local-name').textContent = file.name;
      $('local-help').hidden = true;
      await editor.load(current.payload);
      format.querySelector('option[value="project"]').disabled = !current.source?.song;
      $('local-open-audio').disabled = false;
      $('bsave').disabled = false;
      $('local-format').disabled = false;
      if (format.selectedOptions[0]?.disabled) format.value = 'json';
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
        case 'lyrics': blob = new Blob([lyricText(payload.moras)], { type: 'text/plain;charset=utf-8' }); extension = '.txt'; break;
        case 'project': blob = new Blob([JSON.stringify(projectDocument(current, payload.moras), null, 1)], { type: 'application/json' }); extension = '.project.json'; break;
        default: blob = new Blob([JSON.stringify(saveDocument(current, payload), null, 1)], { type: 'application/json' }); extension = '.json';
      }
      download(blob, filename + '.edited' + extension);
      // MIDI/text are interchange exports, not a lossless save of the editor state.
      if (format.value === 'json') editor.markSaved();
      setStatus('保存しました: ' + filename + '.edited' + extension);
    } catch (error) { setStatus('保存できません: ' + error.message); }
  });

  $('bsave').disabled = true;
  $('local-format').disabled = true;
  $('local-open-audio').disabled = true;
  await editor.load({ moras: [], reference: [], line_texts: {}, ignore_ranges: [], has_audio: false });
  setStatus('楽譜JSON・歌詞入りMIDIを開いてください');

  // The source export is requested only when the user explicitly opens this
  // editor from a completed Video job. No upload or synthesis request is made.
  const job = new URL(location.href).searchParams.get('job');
  if (job && /^[a-zA-Z0-9_-]{1,80}$/u.test(job)) {
    try {
      const response = await fetch('/api/jobs/' + encodeURIComponent(job) + '/exports/midi');
      if (!response.ok) throw new Error('曲の保存期限またはログイン状態を確認してください。');
      const file = new File([await response.blob()], 'song.mid', { type: 'audio/midi' });
      await openFile(file);
    } catch (error) { setStatus('曲を開けません: ' + error.message); }
  }
}
