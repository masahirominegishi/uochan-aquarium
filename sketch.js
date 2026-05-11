// =============================================================
// sketch.js : p5.js のメインスケッチ
//
// 役割
//   - setup() で初期化、draw() で毎フレーム描画
//   - 水槽の背景（水の色・光のゆらめき・水草・気泡）を描く
//   - Fish インスタンスを動かす
//   - 将来の音声連携用に window.aquarium という外部 API を用意する
//
// p5.js のキホン（初心者向けメモ）
//   - setup() : 起動時に1回だけ呼ばれる
//   - draw()  : 毎フレーム（通常 60fps）呼ばれる
//   - createCanvas(w,h) で <canvas> が生成される
//   - 座標系：左上が (0, 0)、右下が (width, height)
// =============================================================

// ---- グローバル変数 ----
let mainFish;        // うおちゃん本体 (Fish インスタンス)
let guestFishes = [];// お客さんがアップロードした魚 (GuestFish インスタンスの配列)
let bubbles = [];    // 気泡たち
let plants  = [];    // 水草の位置情報
let partsConfig;     // assets/uochan/parts.json
let parts = {};      // { imageName: p5.Image } : 12 パーツの透過 PNG

// ゲスト魚が水槽に入るときの効果音 (drop → into を続けて鳴らす)。
// p5.sound は使わず素の HTMLAudioElement で再生する (preload をブロックしない / ライブラリ不要)。
let sndDrop = null;  // sound/drop.mp3
let sndInto = null;  // sound/into.mp3
// 起動直後の初期同期 (bridge が既存の魚を全部 fish_added で送ってくる) では鳴らさないためのフラグ。
// ページ表示から数秒経つまで false にしておく。
let _soundReady = false;

// うおちゃんの 12 パーツ。preload で全部読み込む。
const UOCHAN_IMAGES = [
  'body', 'head',
  'mouth_closed', 'mouth_half', 'mouth_open',
  'eye_open', 'eye_half', 'eye_closed',
  'arm_l', 'arm_r', 'leg_l', 'leg_r',
];

// =============================================================
// preload : setup より前に呼ばれる。画像などのアセットをここで読む
// =============================================================
function preload() {
  partsConfig = loadJSON('assets/uochan/parts.json');
  for (const name of UOCHAN_IMAGES) {
    parts[name] = loadImage(`assets/uochan/${name}.png`);
  }
}

// =============================================================
// setup : 最初の1回だけ実行
// =============================================================
function setup() {
  // ウィンドウサイズいっぱいのキャンバスを作成
  createCanvas(windowWidth, windowHeight);

  // 楕円の中心を「左上」ではなく「中央」基準にする（魚の描画を素直に書きたいので）
  ellipseMode(CENTER);

  // パーツリギングでうおちゃん本体を作成
  mainFish = new Fish(parts, partsConfig);

  // 気泡を初期配置（最初から画面に少しある状態にしたい）
  for (let i = 0; i < 12; i++) {
    bubbles.push(_makeBubble(random(height)));
  }

  // 水草の位置を決める（手前用と奥用を分けて、奥行きを出す）
  for (let i = 0; i < 6; i++) {
    plants.push({
      x: random(width),
      h: random(80, 200),       // 高さ
      sway: random(TWO_PI),     // 揺れの初期位相
      back: true,               // 奥側
    });
  }
  for (let i = 0; i < 4; i++) {
    plants.push({
      x: random(width),
      h: random(140, 260),
      sway: random(TWO_PI),
      back: false,              // 手前側（魚より前に描く）
    });
  }

  // 効果音を読み込む (素の Audio。失敗しても水槽描画には影響しない)
  try {
    sndDrop = new Audio('sound/drop.mp3');
    sndInto = new Audio('sound/into.mp3');
    sndDrop.preload = 'auto';
    sndInto.preload = 'auto';
    // drop の再生が終わったら続けて into を鳴らす
    sndDrop.addEventListener('ended', () => {
      sndInto.currentTime = 0;
      sndInto.play().catch(() => {});
    });
  } catch (e) {
    console.warn('[aquarium] 効果音の初期化に失敗', e);
    sndDrop = sndInto = null;
  }

  // 起動直後の初期同期 (接続時に既存の魚が一気に fish_added で飛んでくる) で効果音が
  // 鳴らないように、表示から少し経ってから効果音を解禁する。
  setTimeout(() => { _soundReady = true; }, 4000);
}

// ゲスト魚が水槽に入る瞬間の効果音: drop → (ended で) into。
// ブラウザの autoplay 制限により、ページに一度もユーザー操作がない状態だと play() は
// 拒否される。フルスクリーンボタンを一度押せば以降は鳴る。拒否時は静かに無視する。
function _playGuestFishEnterSound() {
  if (!_soundReady || !sndDrop) return;
  try {
    sndDrop.currentTime = 0;
    sndDrop.play().catch(() => {});
  } catch (e) {
    /* ignore */
  }
}

// =============================================================
// draw : 毎フレーム実行（60fps が目安）
// =============================================================
function draw() {
  // 1) 水の背景（上から下へグラデーション）
  _drawWaterBackground();

  // 2) 光のゆらめき（上から差し込む光をうっすら）
  _drawLightRays();

  // 3) 奥側の水草（魚より後ろに描く）
  _drawPlants(true);

  // 4) 気泡を更新＆描画
  _updateAndDrawBubbles();

  // 5) 魚を更新 → 描画。レイヤー (奥→手前):
  //      奥  : 飼い主不在 / 未登録の小さいゲスト魚
  //      中  : 飼い主在席 / 今日新しく入った大きいゲスト魚
  //      手前: うおちゃん本体
  mainFish.update();
  for (const g of guestFishes) g.update();
  for (const g of guestFishes) { if (!g.isBig()) g.draw(); }   // 奥
  for (const g of guestFishes) { if (g.isBig()) g.draw(); }    // 中
  mainFish.draw();                                              // 手前

  // 6) 手前側の水草（魚より前に描いてパララックス感を出す）
  _drawPlants(false);

  // 7) ガラス越しっぽい軽い反射（任意・お好みでコメントアウト可）
  _drawGlassOverlay();
}

// -------------------------------------------------------------
// 背景：水のグラデーション
// -------------------------------------------------------------
function _drawWaterBackground() {
  // 上：明るい水色 / 下：深い青
  const top    = color(120, 200, 230);
  const bottom = color(10, 50, 100);
  for (let y = 0; y < height; y++) {
    const t = y / height;
    stroke(lerpColor(top, bottom, t));
    line(0, y, width, y);
  }
}

// -------------------------------------------------------------
// 光のゆらめき：上から差し込む光のすじ
// -------------------------------------------------------------
function _drawLightRays() {
  noStroke();
  const rayCount = 6;
  for (let i = 0; i < rayCount; i++) {
    const baseX = (width / rayCount) * i + (width / rayCount) * 0.5;
    // noise() で滑らかに左右に揺らす
    const offset = (noise(i * 10, frameCount * 0.003) - 0.5) * 120;
    const x = baseX + offset;
    fill(255, 255, 255, 18);   // とても薄い白
    // 上が細く下が広がる台形（光のすじ）
    quad(x - 6, 0, x + 6, 0, x + 80, height, x - 80, height);
  }
}

// -------------------------------------------------------------
// 水草：底から生えて、ゆらゆら揺れる
// back=true なら奥側（暗め）、false なら手前側（濃いシルエット）
// -------------------------------------------------------------
function _drawPlants(back) {
  noStroke();
  for (const p of plants) {
    if (p.back !== back) continue;
    const sway = sin(frameCount * 0.02 + p.sway) * 8;

    if (back) {
      fill(20, 80, 60, 160);   // 奥は青緑で薄め
    } else {
      fill(10, 40, 30, 220);   // 手前は濃いシルエット
    }

    // 葉っぱを縦に何枚か並べる
    push();
    translate(p.x, height);
    for (let i = 0; i < 5; i++) {
      const y = -i * (p.h / 5);
      const w = back ? 14 : 18;
      ellipse(sway * (i / 5), y, w, p.h / 4);
    }
    pop();
  }
}

// -------------------------------------------------------------
// 気泡
// -------------------------------------------------------------
function _makeBubble(startY) {
  return {
    x: random(width),
    y: startY ?? height + 10,
    r: random(2, 6),
    speed: random(0.6, 1.6),
    drift: random(TWO_PI),
  };
}

function _updateAndDrawBubbles() {
  noStroke();
  fill(255, 255, 255, 120);
  for (const b of bubbles) {
    // 左右にゆらゆらしながら上昇
    b.x += sin(frameCount * 0.05 + b.drift) * 0.4;
    b.y -= b.speed;
    ellipse(b.x, b.y, b.r * 2);
  }
  // 画面上に抜けたら下から出し直す
  for (let i = bubbles.length - 1; i >= 0; i--) {
    if (bubbles[i].y < -10) bubbles[i] = _makeBubble();
  }
  // 自然に増減する余裕を持たせる（今は固定数）
}

// -------------------------------------------------------------
// ガラス越しの反射（うっすら）
// -------------------------------------------------------------
function _drawGlassOverlay() {
  noStroke();
  fill(255, 255, 255, 8);
  rect(0, 0, width, height * 0.4);  // 上半分だけわずかに明るく
}

// =============================================================
// ウィンドウサイズが変わったらキャンバスも追従
// =============================================================
function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
}

// =============================================================
// 外部 (uochan-aquarium bridge) からのイベント受信窓口。
//
// イベントは 2 系統:
//   zone 由来 (カメラ): approach / speak / leave / idle
//   AI 由来 (realtime_loop): ai_speak_start / ai_speak_end
//
// 両系統を独立に追跡し、AI 発話中は zone に関わらず魚は 'speak' 状態 (=口パク) を優先。
// AI が黙ったら zone 状態に戻る。
// =============================================================
let _zoneState = 'idle';   // zone 系の最新状態 (approach/speak/leave/idle)
let _aiSpeaking = false;   // AI 発話中か

window.aquarium = {
  onEvent(type, payload = {}) {
    console.log('[aquarium] event:', type, payload);
    switch (type) {
      case 'approach':
      case 'leave':
      case 'idle':
        _zoneState = type;
        break;
      case 'speak':
        // zone 由来 speak (5秒滞在で発火) は無視。口パク同期は AI 由来 (ai_speak_start) が担当する。
        // 客がゾーン内にいる事実は approach として扱えば十分なので _zoneState は変更しない。
        break;
      case 'ai_speak_start':
        _aiSpeaking = true;
        break;
      case 'ai_speak_end':
        _aiSpeaking = false;
        break;
      case 'fish_added':
        // 接続時の初期同期 + 新規アップロード時に bridge から飛んでくる
        this.addGuestFish(payload.image_url, {
          id: payload.id,
          ownerPersonId: payload.owner_person_id,
        });
        return;  // _apply() は不要 (うおちゃん本体の状態は変わらない)
      case 'fish_owner_updated':
        // Phase 1.5: 既存ゲスト魚の飼い主が確定/変更したとき
        for (const g of guestFishes) {
          if (g.id === payload.id) g.setOwnerPersonId(payload.owner_person_id);
        }
        return;
      case 'fish_removed':
        // 管理 UI から削除されたとき: guestFishes から該当魚を除去
        guestFishes = guestFishes.filter((g) => g.id !== payload.id);
        console.log(`[aquarium] guest fish removed: ${payload.id} (total ${guestFishes.length})`);
        return;
      case 'fish_owner_present':
        // Phase 1.5: 飼い主候補が水槽前に来た / いなくなった
        // payload.fish_ids に入っているものだけ前面化、それ以外は解除
        {
          const ids = new Set(payload.fish_ids || []);
          for (const g of guestFishes) g.setHighlighted(ids.has(g.id));
        }
        return;
      default:
        console.warn('[aquarium] unknown event type:', type);
        return;
    }
    this._apply();
  },

  _apply() {
    const next = _aiSpeaking ? 'speak' : _zoneState;
    mainFish.setState(next);
  },

  // 動作確認用：ブラウザの DevTools コンソールから手で叩けるようにしておく
  //   例：aquarium.test('approach')
  test(type) { this.onEvent(type); },

  // ゲスト魚を追加 (ws-client.js から呼ばれる)
  // imageUrl から非同期に画像を読み込み、GuestFish として guestFishes 配列に push
  addGuestFish(imageUrl, options = {}) {
    // 同じ id が既に存在すれば追加せず ownerPersonId だけ更新する (welcome 多重防止)
    if (options.id) {
      const existing = guestFishes.find((g) => g.id === options.id);
      if (existing) {
        if (options.ownerPersonId !== undefined) existing.setOwnerPersonId(options.ownerPersonId);
        return;
      }
    }
    loadImage(
      imageUrl,
      (img) => {
        // _soundReady が立っている = 起動直後の初期同期ではなく「いま新しく登録された魚」。
        // 新規登録のときだけ「上からドボン」のドロップイン + 着水時に効果音。
        // 初期同期(_soundReady=false の間)はドロップなし・音なしでその場に出現 (従来通り)。
        const isNewArrival = _soundReady;
        const fish = new GuestFish(img, {
          ...options,
          dropIn: isNewArrival,
          onSplash: isNewArrival ? () => _playGuestFishEnterSound() : null,
        });
        guestFishes.push(fish);
        console.log(
          `[aquarium] guest fish added: ${fish.id} (total ${guestFishes.length})` +
          (isNewArrival ? ' [drop-in]' : '')
        );
      },
      (err) => {
        console.warn(`[aquarium] failed to load guest fish image: ${imageUrl}`, err);
      }
    );
  },

  // テスト用: うおちゃんの body 画像を流用してゲスト魚を 1 匹追加
  testAddGuest() {
    this.addGuestFish('assets/uochan/body.png');
  },
};
