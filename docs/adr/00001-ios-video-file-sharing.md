# ADR 00001: 完成動画の再生・保存・共有

- Status: accepted

## Decision

完成動画は、端末が file sharing に対応する場合、同じ MP4 file を再生と共有に利用します。

- iPhone、iPad、Android では「動画を保存・共有」を表示します。
- PC では「動画をダウンロード」を表示し、file sharing に対応する場合は共有も別に表示します。
- 共有は利用者の明示的な操作から開始します。
- 共有の準備または再生に失敗した場合は、直接再生・直接 download へ fallback します。
- 画面を離れた場合は、準備中の取得と一時 URL を解放します。
- 共有の取消は error として表示しません。それ以外の共有 error では直接 download を案内します。

この方針は、対応端末で動画 file 付き共有を提供しつつ、共有 API を利用できない環境にも
保存手段を残すための最終仕様です。

## References

- [W3C Web Share API](https://w3c.github.io/web-share/)
- [W3C File API](https://w3c.github.io/FileAPI/)
