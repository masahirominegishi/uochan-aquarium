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
let plants  = [];    // 水草: scene.json から構築された描画用エントリ配列
let rig;             // assets/uochan/rig.json (swim / talk 2セットのリグ定義)
let swimImgs = {};   // { name -> p5.Image } : 泳ぐ魚 10パーツ
let talkImgs = {};   // { name -> p5.Image } : 話す魚 14パーツ
let scene;           // assets/scene/scene.json (背景・水草のレイアウト定義)
let bgFarImg;        // 遠景: bg_far.png (水のグラデと水底)
let sceneImgs = {};  // { filename -> p5.Image } : 切り抜いた水草パーツ

// ゲスト魚が水槽に入るときの効果音 (drop → into を続けて鳴らす)。
// p5.sound は使わず素の HTMLAudioElement で再生する (preload をブロックしない / ライブラリ不要)。
let sndDrop = null;  // sound/drop.mp3
let sndInto = null;  // sound/into.mp3

// 起動直後の初期同期: bridge が既存の魚を全部 fish_added で送ってくる。
// これを「ぜんぶ一斉にポンと出す」のではなく、キューに溜めて 1 匹ずつ時間差で
// 「上からドロップイン」させる (音なし / サイズ・レイヤーは通常)。
//   millis() < _STARTUP_WINDOW_MS の間に来た fish_added → キュー行き (= 初期同期の既存魚)
//   それ以降に来た fish_added                          → 即ドロップイン + 着水音 (= ライブの新規登録)
const _STARTUP_WINDOW_MS         = 3000;
const _STARTUP_SPAWN_INTERVAL_MS = 500;
let _startupQueue       = [];     // [{ imageUrl, options }]
let _lastStartupSpawnAt = -_STARTUP_SPAWN_INTERVAL_MS;  // 最初の 1 匹はすぐ出す
let _startupSpawnIdx    = 0;      // golden-ratio で x をばらけさせるためのカウンタ

// うおちゃん本体のパーツ名 (preload で全部読み込む)。実体は assets/uochan_swim/ と assets/uochan_talk/。
const SWIM_PART_NAMES = [
  'body', 'fin',
  'arm_l', 'arm_l_hand', 'arm_r', 'arm_r_hand',
  'leg_l', 'leg_l_shin', 'leg_r', 'leg_r_shin',
];
const TALK_PART_NAMES = [
  'body', 'fin',
  'eye_open', 'eye_closed', 'mouth_open', 'mouth_closed',
  'arm_l', 'arm_l_hand', 'arm_r', 'arm_r_hand',
  'leg_l', 'leg_l_shin', 'leg_r', 'leg_r_shin',
];

// 水槽シーンの水草 PNG 一覧。scene.json は preload 時点ではまだ JSON として
// 参照できる保証がないので (p5 の loadJSON は preload 内で並行ロード)、
// 画像ロードはここに列挙した固定リストで行う。
// 命名規則: plants_<layer>_<id>[_<part>].png。scene.json と一致させること。
const SCENE_PLANT_FILES = [
  'plants_back_01_a.png', 'plants_back_01_b.png',
  'plants_back_02.png', 'plants_back_03.png', 'plants_back_04.png', 'plants_back_05.png',
  'plants_back_06_1.png', 'plants_back_06_2.png', 'plants_back_06_3.png',
  'plants_front_07_1.png', 'plants_front_07_2.png', 'plants_front_07_3.png',
];

// 株ごとの揺れ振幅 (ラジアン)。0 を返すと静止。
// サンゴ系 (01 の塊 / 04 の小さな花) は回転すると不自然なので静止。
// 05 の大きな青ファンも形がしっかりしているので静止。
// 02 / 03 の黄緑のわかめ系と、06_ / 07_ の細長い葉だけ揺らす。
function _plantSwayAmp(filename) {
  if (/_0[23]\./.test(filename)) return 0.10;  // 黄緑わかめ: 短いが柔らかく見えるよう少し大きめ
  if (/_06_/.test(filename))     return 0.08;  // 奥の細長い葉
  if (/_07_/.test(filename))     return 0.12;  // 手前の細長い葉 (パララックスで強め)
  return 0;                                     // それ以外 (サンゴ等) は静止
}

// =============================================================
// preload : setup より前に呼ばれる。画像などのアセットをここで読む
// =============================================================
function preload() {
  rig = loadJSON('assets/uochan/rig.json');
  for (const name of SWIM_PART_NAMES) swimImgs[name] = loadImage(`assets/uochan_swim/${name}.png`);
  for (const name of TALK_PART_NAMES) talkImgs[name] = loadImage(`assets/uochan_talk/${name}.png`);

  // 水槽シーン (背景 + 水草)
  scene    = loadJSON('assets/scene/scene.json');
  bgFarImg = loadImage('assets/scene/bg_far.png');
  for (const f of SCENE_PLANT_FILES) sceneImgs[f] = loadImage(`assets/scene/${f}`);
}

// =============================================================
// setup : 最初の1回だけ実行
// =============================================================
function setup() {
  // ウィンドウサイズいっぱいのキャンバスを作成
  createCanvas(windowWidth, windowHeight);

  // 60fps だと Pi 5 の CPU(canvas2d 描画)を 1.8 コアぶん回してファンが全開になる。
  // 水槽の動きはゆったりなので 30fps で十分。CPU ≒ 半分、温度・ファンが下がる。
  frameRate(30);

  // 楕円の中心を「左上」ではなく「中央」基準にする（魚の描画を素直に書きたいので）
  ellipseMode(CENTER);

  // パーツリギングでうおちゃん本体を作成 (swim / talk 2セット)
  mainFish = new Fish(rig, { swim: swimImgs, talk: talkImgs });

  // 気泡を初期配置（最初から画面に少しある状態にしたい）
  for (let i = 0; i < 12; i++) {
    bubbles.push(_makeBubble(random(height)));
  }

  // 水草を scene.json から構築。各エントリは PNG とアンカー (元 1920x1080 上の
  // 根元位置) を持つ。描画時は anchor を画面サイズにスケールして配置する。
  // sway フラグが立っているものは根元を軸に sin で微小回転する。
  const _buildPlant = (entry, layer) => ({
    img:      sceneImgs[entry.file],
    layer:    layer,            // 'back' or 'front'
    crop_x:   entry.crop_x,     // 元キャンバス上の切り抜き左上 x
    crop_y:   entry.crop_y,     // 同 y
    crop_w:   entry.crop_w,
    crop_h:   entry.crop_h,
    anchor_x: entry.anchor_x,   // 元キャンバス上の根元 x (株中央)
    anchor_y: entry.anchor_y,   // 同 y (株下端)
    swayAmp:  _plantSwayAmp(entry.file),  // 0 なら静止
    phase:    random(TWO_PI),   // 揺れの初期位相 (株ごとにばらける)
  });
  for (const e of scene.plants_back)  plants.push(_buildPlant(e, 'back'));
  for (const e of scene.plants_front) plants.push(_buildPlant(e, 'front'));

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

  // 起動時の既存魚は draw() 内のディスペンサが _startupQueue から 1 匹ずつ取り出して
  // 時間差でドロップインさせる (音なし)。_STARTUP_WINDOW_MS を過ぎてから来た fish_added は
  // ライブの新規登録扱いで即ドロップイン + 着水音。
}

// ゲスト魚が水槽に入る瞬間の効果音: drop → (ended で) into。
// ブラウザの autoplay 制限により、ページに一度もユーザー操作がない状態だと play() は
// 拒否される。フルスクリーンボタンを一度押せば以降は鳴る。拒否時は静かに無視する。
function _playGuestFishEnterSound() {
  if (!sndDrop) return;
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

  // 4.5) 起動時の既存魚をキューから 1 匹ずつ時間差でドロップインさせる (音なし)。
  //      x は golden-ratio の低食い違い列で画面幅に均等ばらけ + わずかなジッタ。
  if (_startupQueue.length > 0 && millis() - _lastStartupSpawnAt > _STARTUP_SPAWN_INTERVAL_MS) {
    const item = _startupQueue.shift();
    const PHI = 0.6180339887;
    const fx = width * (0.10 + 0.80 * ((_startupSpawnIdx * PHI) % 1)) + random(-width * 0.03, width * 0.03);
    _startupSpawnIdx += 1;
    window.aquarium._spawnGuestFish(item.imageUrl, item.options, { startupEntry: true, withSound: false, x: fx });
    _lastStartupSpawnAt = millis();
  }

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
// 背景：bg_far.png を画面いっぱいに描く
// -------------------------------------------------------------
// 1920x1080 で描かれた背景イラストを、現在の canvas サイズに 1 回ブリットする。
// 旧実装 (手続きの縦グラデ) は createLinearGradient + fillRect の 1 コールで
// 既に軽かったが、こちらも image() 1 コールなので CPU 的にトントン or 安い。
function _drawWaterBackground() {
  image(bgFarImg, 0, 0, width, height);
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
// 水草：scene.json の配置を画面にスケールして描画。
// back=true は奥のレイヤー (魚より後ろ)、false は手前 (魚より前)。
// sway フラグが立っている株 (06_/07_ の細長い葉) は根元を軸に微小回転。
// -------------------------------------------------------------
function _drawPlants(back) {
  const layer = back ? 'back' : 'front';
  const sx = width  / scene.canvas.w;  // 元 1920 -> canvas 幅
  const sy = height / scene.canvas.h;  // 元 1080 -> canvas 高さ

  for (const p of plants) {
    if (p.layer !== layer) continue;

    // 画面上のアンカー (根元) 座標
    const ax = p.anchor_x * sx;
    const ay = p.anchor_y * sy;

    // 画像内での根元の相対位置 (左上からの距離)
    const localAnchorX = (p.anchor_x - p.crop_x);
    const localAnchorY = (p.anchor_y - p.crop_y);

    push();
    translate(ax, ay);
    if (p.swayAmp > 0) {
      // sin で根元軸まわりに微小回転。振幅は _plantSwayAmp で株ごとに決定。
      const angle = sin(frameCount * 0.02 + p.phase) * p.swayAmp;
      rotate(angle);
    }
    image(
      p.img,
      -localAnchorX * sx, -localAnchorY * sy,
      p.crop_w * sx, p.crop_h * sy
    );
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
// AI が話し終えても即 zone 状態 (泳ぎ) には戻さず、AI_LINGER_MS だけ 'attentive'
// (talk セットのまま・口は閉じる) で会話モードのまま待機させてから zone 状態へ戻す。
// =============================================================
let _zoneState = 'idle';   // zone 系の最新状態 (approach/speak/leave/idle)
let _aiSpeaking = false;   // AI 発話中か
let _aiLingerUntil = 0;    // ai_speak_end の後、この millis() まで 'attentive' (口を閉じて会話モードのまま待機)
const AI_LINGER_MS = 5000; // ↑ 余韻の長さ。話し終わってから泳ぎに戻すまでの間 (ms)。

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
        _aiLingerUntil = 0;                       // 次のターン: 余韻待ちは打ち切って speak へ
        break;
      case 'ai_speak_end':
        _aiSpeaking = false;
        _aiLingerUntil = millis() + AI_LINGER_MS; // 話し終わり → しばらく会話モードのまま (口は閉じて) 待機
        setTimeout(() => window.aquarium._apply(), AI_LINGER_MS + 50);  // 余韻が切れたら泳ぎ (zone 状態) に戻す
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
    let next;
    if (_aiSpeaking)                    next = 'speak';
    else if (millis() < _aiLingerUntil) next = 'attentive';   // 話し終わり後の余韻 (口を閉じて会話モードのまま待機)
    else                                next = _zoneState;
    mainFish.setState(next);
  },

  // 動作確認用：ブラウザの DevTools コンソールから手で叩けるようにしておく
  //   例：aquarium.test('approach')
  test(type) { this.onEvent(type); },

  // ゲスト魚を追加 (ws-client.js から呼ばれる)
  // imageUrl から非同期に画像を読み込み、GuestFish として guestFishes 配列に push
  addGuestFish(imageUrl, options = {}) {
    // 同じ id が既に居る / キューに居る → 追加せず ownerPersonId だけ更新 (welcome 多重防止)
    if (options.id) {
      const existing = guestFishes.find((g) => g.id === options.id);
      if (existing) {
        if (options.ownerPersonId !== undefined) existing.setOwnerPersonId(options.ownerPersonId);
        return;
      }
      if (_startupQueue.some((q) => q.options && q.options.id === options.id)) return;
    }
    // 起動直後のウィンドウ中に来た fish_added は「初期同期の既存魚」とみなしてキューへ
    // (draw() のディスペンサが時間差でドロップインさせる、音なし)。
    // それ以降に来たものは「ライブの新規登録」として即ドロップイン + 着水音。
    if (millis() < _STARTUP_WINDOW_MS) {
      _startupQueue.push({ imageUrl, options });
      return;
    }
    this._spawnGuestFish(imageUrl, options, { dropIn: true, withSound: true });
  },

  // 画像を読み込んで GuestFish を生成する共通ルーチン。
  //   opts: { dropIn?, startupEntry?, withSound?, x? }
  _spawnGuestFish(imageUrl, options = {}, opts = {}) {
    loadImage(
      imageUrl,
      (img) => {
        const fish = new GuestFish(img, {
          ...options,
          ...(opts.x !== undefined ? { x: opts.x } : {}),
          dropIn: !!opts.dropIn,
          startupEntry: !!opts.startupEntry,
          onSplash: opts.withSound ? () => _playGuestFishEnterSound() : null,
        });
        guestFishes.push(fish);
        const tag = opts.dropIn ? ' [drop-in]' : (opts.startupEntry ? ' [startup]' : '');
        console.log(`[aquarium] guest fish added: ${fish.id} (total ${guestFishes.length})${tag}`);
      },
      (err) => {
        console.warn(`[aquarium] failed to load guest fish image: ${imageUrl}`, err);
      }
    );
  },

  // テスト用: うおちゃん泳ぐ魚の body 画像を流用してゲスト魚を 1 匹追加
  testAddGuest() {
    this.addGuestFish('assets/uochan_swim/body.png');
  },
};
