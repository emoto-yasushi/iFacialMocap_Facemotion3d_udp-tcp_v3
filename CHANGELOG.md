# Changes / 変更点

[README](README.md) · [Specification](PROTOCOL_V3.md) · [仕様書](PROTOCOL_V3_JA.md)

## English

### Full-screen ads (iFacialMocapTr 1.2.9, revision 6)

The message format and the revision do not change. A receiver (PC) that follows the specification needs no change.

- When a full-screen ad covers the tracking screen during a UDP session, iOS sends ERROR `APP_INACTIVE` and ends the session. Earlier versions stopped sending without a message.
- When the ad closes, iOS starts a new session by itself. For a session that a receiver (PC) started, iOS sends to the same address and port as a manual start (`client_nonce = 0`), with the same mode, and with `actual_fps` and `max_udp_size` at most those of the original HELLO. Earlier versions did not start again, so the receiver (PC) had to send a new HELLO.
- In WAITING, accept this SCHEMA as a new session, and reset the stored SCHEMA and the last FRAME `sequence` ([section 2](PROTOCOL_V3.md#udp), [section 5](PROTOCOL_V3.md#timers)).
- TCP does not change.

### Revision 6 (the first App Store release of v3)

- SCHEMA is binary (no JSON). HELLO has a new `requested_mode` field.
- New fixed52 mode: the 52 ARKit values as Int32 in a fixed order, without names (SCHEMA 66 bytes, FRAME 296 bytes, never split).
- Revision 5 and revision 6 do not work together, and retries do not help:
  - A revision-6 app refuses a HELLO with another revision with ERROR `UNSUPPORTED_CONTRACT: requires 6`. It never switches to the revision-5 format (JSON).
  - A revision-5 app drops a revision-6 HELLO as malformed (over TCP it closes the connection), so no reason comes back.
  - On a manual start from a revision-5 app, the reference receiver prints `iOS uses FMV3 contract revision 5; this receiver needs revision 6`.

### Revisions 4 and 5

Used only by beta builds before the first App Store release of v3.

## Japanese

### 全画面広告（iFacialMocapTr 1.2.9、revision 6）

メッセージの形式とrevisionは変わりません。仕様どおりに作られた受信側（PC）は、変更の必要はありません。

- UDPのセッション中に全画面広告がトラッキング画面を隠すと、iOSはERROR `APP_INACTIVE`を送ってセッションを終えます。以前のバージョンは、何も知らせずに送信を止めていました。
- 広告が閉じると、iOSが自動で新しいセッションを始めます。受信側（PC）から始めたセッションでは、同じアドレス・ポートへ、手動開始と同じ形（`client_nonce = 0`）で送ります。モードは同じで、`actual_fps`と`max_udp_size`は元のHELLO以下です。以前のバージョンは始まり直さなかったので、受信側（PC）から新しくHELLOを送る必要がありました。
- WAITINGでは、このSCHEMAを新しいセッションとして受け付け、保存したSCHEMAと最後のFRAMEの`sequence`をリセットします（[2章](PROTOCOL_V3_JA.md#udp)、[5章](PROTOCOL_V3_JA.md#timers)）。
- TCPは変わりません。

### revision 6（v3の最初のApp Store公開）

- SCHEMAがバイナリになりました（JSONは使いません）。HELLOに`requested_mode`が加わりました。
- fixed52モードが加わりました。52個のARKitの値を、決まった順番のInt32で、名前なしで送ります（SCHEMAは66バイト、FRAMEは296バイトで、分割されません）。
- revision 5とrevision 6は一緒に動かず、再試行しても接続できません。
  - revision 6のアプリは、ほかのrevisionのHELLOをERROR `UNSUPPORTED_CONTRACT: requires 6`で断ります。revision 5の形式（JSON）に切り替えることはありません。
  - revision 5のアプリは、revision 6のHELLOを不正な形式として捨てます（TCPでは接続を閉じます）。そのため理由は返ってきません。
  - revision 5のアプリから手動開始すると、参照受信側は`iOS uses FMV3 contract revision 5; this receiver needs revision 6`と表示します。

### revision 4と5

v3がApp Storeで最初に公開される前のベータ版だけで使われました。
