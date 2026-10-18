# ADR 00001: 完成動画の再生・保存・共有

- Status: accepted

## Decision

完成動画は、端末が file sharing に対応する場合、同じ MP4 file を再生と共有に利用します。

- 端末を問わず「保存」メニューと「共有」を分けて表示します。共有非対応の環境では保存だけを表示します。
- 保存メニューでは動画、MusicXML、歌詞入りMIDI、XF MIDI、伴奏なし歌声WAVから利用できる形式を選べます。
- 共有は利用者の明示的な操作から開始します。準備後も操作権限が有効なら、そのまま共有画面を開きます。
  操作権限が失効した場合は準備したファイルを保持し、「共有を開く」で再タップを案内します。
- 共有の準備または再生に失敗した場合は、直接再生・直接 download へ fallback します。
- 画面を離れた場合は、準備中の取得と一時 URL を解放します。
- 共有の取消は error として表示しません。それ以外の共有 error では直接 download を案内します。

この方針は、対応端末で動画 file 付き共有を提供しつつ、共有 API を利用できない環境にも
保存手段を残すための最終仕様です。

## References

- [W3C Web Share API](https://w3c.github.io/web-share/)
- [W3C File API](https://w3c.github.io/FileAPI/)
