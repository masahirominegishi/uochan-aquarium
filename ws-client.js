// =============================================================
// ws-client.js : 水槽 ↔ ブリッジの WebSocket クライアント
//
// 役割
//   - pi-main 上で動く uochan-aquarium ブリッジ (ポート 8765) に接続
//   - 受信した {type, payload} を window.aquarium.onEvent(...) に流し込む
//   - 切断時は 5 秒間隔で自動再接続（カメラ死亡などからの復帰）
//
// 接続先の決め方
//   1. URL クエリ ?ws=ws://host:port/ があればそれを優先
//   2. file:// で開いている (Mac 開発時) → raspberrypi.local:8765
//      （pi-main は SSH エイリアスで mDNS 名ではないため使わない）
//   3. それ以外 → 同じホスト名の :8765
//      pi-main の kiosk は http://localhost:8080 で開くのでここに入り
//      ws://localhost:8765 になる。以前は localhost を Mac 開発扱いして
//      raspberrypi.local (mDNS) に繋いでいたが、ブート直後は avahi が
//      間に合わず接続失敗の温床だった (2026-07-26 修正)。
// =============================================================

(function () {
  const params = new URLSearchParams(location.search);
  const explicit = params.get('ws');
  const host = location.hostname;
  const isFile = location.protocol === 'file:' || host === '';

  const wsUrl =
    explicit ||
    (isFile ? 'ws://raspberrypi.local:8765/' : `ws://${host}:8765/`);

  let ws = null;
  let reconnectTimer = null;

  function connect() {
    console.log(`[ws-client] connecting to ${wsUrl}`);
    ws = new WebSocket(wsUrl);

    ws.addEventListener('open', () => {
      console.log('[ws-client] connected');
    });

    ws.addEventListener('message', (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (window.aquarium && typeof window.aquarium.onEvent === 'function') {
          window.aquarium.onEvent(data.type, data.payload || {});
        }
      } catch (e) {
        console.warn('[ws-client] bad message', ev.data, e);
      }
    });

    ws.addEventListener('close', () => {
      console.log('[ws-client] disconnected, retrying in 5s');
      scheduleReconnect();
    });

    ws.addEventListener('error', () => {
      // close が続けて来るのでここでは何もしない
    });
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', connect);
  } else {
    connect();
  }
})();
