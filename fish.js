// =============================================================
// Fish クラス: 「うおちゃん本体」をパーツ別 PNG でリギングして合成描画する
//
// 2026-05-12 刷新: 初代うおちゃん (8パーツ・楕円スタブ → 手書きイラスト) を引退させ、
//   2 種のイラストセットを切り替える方式に:
//     swim = 泳ぐ魚 (横向き寝そべり、10パーツ)        … idle / approach / leave のとき
//     talk = 話す魚 (斜め45°立ち、14パーツ + 目/口)   … speak (= AI 発話中) のとき
//   定義は assets/uochan/rig.json。各 limb は 上腕/前腕 (or 腿/脛) の 2 セグメントを
//   肩→肘 (腰→膝) で階層回転させる。poses.rest(=見本1) / p2(≈見本2) / p3(≈見本3) の
//   3 キーポーズを pose_cycle に沿ってゆっくり三角波補間でループ。
//   体は初代と同じく縦スライスの「しなり」、胸ビレはゆるくパタパタ。
//   初代の見た目は assets/uochan_gen1/ に保存 (参照のみ、コードからは未使用)。
//
// 座標/角度の約束 (rig.json と合わせる):
//   - pivot / proximal / distal は各セットのキャンバス画素 (画像左上=原点)、rest ポーズ時点の位置
//   - 角度は度。p5 の rotate() と同じく「正 = 時計回り (画面、y は下向き)」
//
// 状態 (ws-client.js から setState):
//   idle / approach / speak / attentive / leave
//   - 'speak'     : AI 発話中。talk セット + 口パク。
//   - 'attentive' : AI が話し終えた直後の余韻 (sketch.js が ai_speak_end 後 AI_LINGER_MS 保つ)。
//                   talk セットのまま (会話モードの見た目) だが口は閉じたまま動かさない。
// =============================================================

// pose_cycle を 1 周するのにかける基準フレーム数 (30fps 前提。state ごとに speed で割る)
const RIG_CYCLE_FRAMES = 132;

// idle の「ひと休み」: 何回か泳ぐ → 手を後ろに伸ばした (p3 hold) ところで止まる → またしばらく泳ぐ
const IDLE_REST_PHASE   = 0.5;    // 休憩に入る cyclePhase (= p3 hold ≒ 腕を後ろに伸ばしたあたり)
const IDLE_REST_MIN_MS  = 1600;   // ひと休みの長さ (ms, 下限)
const IDLE_REST_MAX_MS  = 4000;   // 〃 (上限)
const IDLE_SWIMS_MIN    = 3;      // 次の休憩までに泳ぐ cycle 数 (下限, 含む)
const IDLE_SWIMS_MAX    = 7;      // 〃 (上限, 含まない → 実際は 3〜6 周)

// ひと休み中、手足がゆっくり下がってくる「だらーん」演出。各 limb の pose 角 [upper, lower] に
// だらーん係数 (0→1, 休憩中はじわっと増・動き出すとじわっと戻る) ぶん足す (正 = 下がる方向)。
const REST_DROOP_RAMP_FRAMES    = 42;   // 休憩中: この frame 数かけて係数 0→1 (~1.4s @30fps)
const REST_DROOP_RECOVER_FRAMES = 16;   // 動き出し: この frame 数かけて係数 1→0 (~0.5s)
const REST_DROOP = {
  arm_l: { u: 38, l: 24 },
  arm_r: { u: 38, l: 24 },
  leg_l: { u: 12, l: 8 },
  leg_r: { u: 12, l: 8 }
};

// swim ⇄ talk の中間フレーム: 切替を緩衝するため、swim rig のまま pose 角を縮めて talk 寄り
// (まっすぐ立った姿勢) に寄せた時間を挟む。時間ベース (ms) でないと state の cycle 速度差で
// 遷移時間が大きく変わって体感が悪い (speak は cycle 速度が遅いので cycle 数だと長すぎる)。
const TALK_TRANSITION_MS         = 500;
const TALK_TRANSITION_POSE_SCALE = 0.4;

// idle 時の縦移動 (GuestFish と同じ仕組み)。approach / leave / speak / attentive では vy=0 で水平維持。
const FISH_TILT_MAX_DEG = 15;    // 進行方向への傾き上限 (兼: 上下動の角度上限)
const FISH_TILT_LERP    = 0.12;  // 傾きの補間係数
const FISH_TURN_MIN_SEC = 2.5;   // 進行角を変える間隔の下限
const FISH_TURN_MAX_SEC = 6.0;   // 〃 上限

class Fish {
  // rig:    rig.json の中身 ({ swim:{...}, talk:{...} })
  // images: { swim: { name -> p5.Image }, talk: { name -> p5.Image } }
  constructor(rig, images) {
    this.rig = rig;

    // セットごとに描画用の前計算 (display スケール / キャンバス中心 / レイヤー一覧 / pose_cycle の区切り境界)
    this.sets = {};
    for (const key of ['swim', 'talk']) {
      const cfg = rig[key];
      const scale = cfg.display_width
        ? cfg.display_width / cfg.canvas_width
        : cfg.display_height / cfg.canvas_height;
      const anchor = cfg.anchor || [cfg.canvas_width / 2, cfg.canvas_height / 2];
      // pose_cycle の各区切りの相対長 (cycle_weights、未指定なら全部 1) → 累積境界 [0, …, 1]
      const keys = cfg.pose_cycle || ['rest'];
      const w = (cfg.cycle_weights && cfg.cycle_weights.length === keys.length) ? cfg.cycle_weights : keys.map(() => 1);
      const total = w.reduce((a, b) => a + b, 0) || 1;
      const breaks = [0];
      for (const wi of w) breaks.push(breaks[breaks.length - 1] + wi / total);
      breaks[breaks.length - 1] = 1;
      this.sets[key] = { cfg, imgs: images[key], scale, anchor, breaks };
    }
    // swim のキック (p2→p3 への区切りの頭) の cyclePhase — velocity surge の基準
    {
      const keys = rig.swim.pose_cycle, bk = this.sets.swim.breaks, n = keys.length;
      let i = -1;
      for (let k = 0; k < n; k++) { if (keys[k] === 'p2' && keys[(k + 1) % n] === 'p3') { i = k; break; } }
      this.swimKickPhase = (i >= 0) ? bk[i] : 1 / 6;
    }
    // 休憩復帰時に飛ばす cyclePhase: p3→p4 segment の頭。
    // 休憩で手が「だらーん」と下がっているところから、p3 (=後ろ) に戻さず p4 (=肘を前にたたむ)
    // への smoothstep に直接乗せて「ぶらーんから前に手を伸ばす」動きにする。
    {
      const keys = rig.swim.pose_cycle, bk = this.sets.swim.breaks, n = keys.length;
      let i = -1;
      for (let k = 0; k < n; k++) { if (keys[k] === 'p3' && keys[(k + 1) % n] === 'p4') { i = k; break; } }
      this.idleRestExitPhase = (i >= 0) ? bk[i] : IDLE_REST_PHASE;
    }

    // 位置・速度 (画面座標)
    this.x  = width * 0.5;
    this.y  = height * 0.5;
    this.vx = -0.6;
    this.vy = 0;
    this.tilt = 0;          // 進行方向への傾き (rad)、idle 中だけ vy 由来で振れる
    this.nextTurnAt = 0;    // idle 中の進行角再抽選タイマー (millis(), 0=初回フレームで即発火)

    // 状態
    this.state = 'idle';
    this.stateChangedAt = millis();
    this.speakStartedAt = 0;

    // talk への寄り具合 (0 = swim rig / 1 = talk rig)。pretalk/posttalk 中は 0 のままで、
    // swim rig の pose 角を縮めて talk 姿勢に寄せる (実切替は talkRigActive で管理)。
    this.talkMix = 0;
    this.talkRigActive = false;          // 実際に talk rig を描いているか
    this.transitionState = null;         // null | 'pretalk' (swim→talk へ向かう中) | 'posttalk' (talk→swim から戻り中)
    this.transitionEndAt = 0;            // 中間フレーム終了時刻 (millis())

    // アニメ位相
    this.tailPhase  = 0;            // しなり
    this.cyclePhase = 0;            // pose_cycle (0..1 で 1 周)
    this.finPhase   = random(TWO_PI);
    this.bobPhase   = random(TWO_PI);

    // idle のひと休み (上記 IDLE_REST_* を参照)
    this.idleCyclesDone     = 0;                                      // 前回の休憩以降に終わった cycle 数
    this.idleSwimsUntilRest = Math.floor(random(IDLE_SWIMS_MIN, IDLE_SWIMS_MAX)); // 次の休憩までに泳ぐ周数
    this.idleRestUntil      = 0;                                      // > 0 = 休憩中で、この millis() まで停止
    this.idleDroop          = 0;                                      // だらーん係数 0..1 (休憩中=増, 動き出し=減)

    // 瞬き (talk)
    this.lastBlinkAt = millis();
    this.nextBlinkInterval = random(3.0, 7.0);
    this.blinkProgress = -1;        // -1 = 開、0..1 = 瞬き中
  }

  // -----------------------------------------------------------
  setState(newState) {
    if (this.state === newState) return;
    const wasTalking = (this.state === 'speak' || this.state === 'attentive');
    const willTalk   = (newState === 'speak'   || newState === 'attentive');
    this.state = newState;
    this.stateChangedAt = millis();
    if (newState === 'speak') this.speakStartedAt = millis();
    if (newState === 'idle') {                  // idle に入り直したらひと休みのカウントをリセット
      this.idleCyclesDone = 0;
      this.idleSwimsUntilRest = Math.floor(random(IDLE_SWIMS_MIN, IDLE_SWIMS_MAX));
      this.idleRestUntil = 0;
      this.nextTurnAt = millis();               // 初回フレームで即 vy 抽選
    } else {
      this.vy = 0;                              // idle 以外は水平維持 (tilt は target=0 で自然に戻る)
    }
    if (newState === 'sleep') {
      this.vx = 0;                              // 寝ているときは横移動も止める
    }
    // swim ⇄ talk の遷移: 双方向で swim rig のまま pose 角を縮めた状態を TALK_TRANSITION_MS だけ挟む。
    // 実切替は millis() で完了させる (update 側)。
    if (!wasTalking && willTalk) {
      this.transitionState = 'pretalk';
      this.transitionEndAt = millis() + TALK_TRANSITION_MS;
      // talkRigActive はまだ false のまま (pretalk 完了時に true へ)
    } else if (wasTalking && !willTalk) {
      if (this.talkRigActive) {
        this.transitionState = 'posttalk';
        this.transitionEndAt = millis() + TALK_TRANSITION_MS;
        this.talkRigActive = false;
      } else {
        // pretalk 途中でキャンセル → そのまま swim へ
        this.transitionState = null;
      }
    }
    console.log(`[Fish] state -> ${newState}${this.transitionState ? ` (${this.transitionState})` : ''}`);
  }

  // 現在「主に見えている」セット名 (壁判定などに使う)
  _dominantSet() { return this.talkMix >= 0.5 ? 'talk' : 'swim'; }

  // state ごとのアニメ速さ
  _speed() {
    switch (this.state) {
      case 'approach': return { cycle: 1.6, wave: 1.7 };
      case 'speak':
      case 'attentive': return { cycle: 0.95, wave: 0.7 };   // 余韻待機も話す時と同じ体の動き (口だけ閉じる)
      case 'leave':    return { cycle: 1.8, wave: 1.9 };
      case 'sleep':    return { cycle: 0,    wave: 0.25 };   // 寝てる: pose を固定 (cycle 進めない)、しなりは弱く呼吸感
      default:         return { cycle: 1.67, wave: 1.6 };  // idle: 「両方伸びてる」区切り(p4→rest hold)をほぼ無くした分だけ全体も短縮 (他の区切りの実時間は据え置き)
    }
  }

  // state ごとの横移動倍率
  // idle: 平泳ぎ風 — キックの瞬間 (cyclePhase≈0.5、p2→p3 の snap) に ぐいっと加速 →
  //       指数減衰で 見本1/見本2 のあたりはほぼ停止 → 次のキックでまた すいーっと進む
  _velocityMul() {
    switch (this.state) {
      case 'speak':
      case 'attentive': return 0.08;                      // ほぼ停止 (発話中 / 話し終わり後の余韻)
      case 'approach': return 1.6;
      case 'leave':    return 2.2;
      default: {
        if (this.idleRestUntil > 0) return 0.04;            // ひと休み中はほぼ停止
        // キックは p2→p3 の区切りの頭 (cyclePhase = this.swimKickPhase)。
        // 蹴った瞬間に大げさに前進 → 最初は速く・急に失速 → 見本3 保持の途中で停止 →
        // 腕の戻し (p3→p4 肘たたみ → p4→rest 伸ばし) と 手を伸ばしてる短い時間 はほぼ停止 → 次のキックでまた
        const sinceKick = ((this.cyclePhase - this.swimKickPhase) % 1 + 1) % 1; // 0 = 蹴った直後
        const t = Math.max(0, 1 - sinceKick / 0.5);
        return 0.05 + 48 * Math.pow(t, 2.6);
      }
    }
  }

  // -----------------------------------------------------------
  // フレーム更新 (canvas は frameRate(30) 固定なのでフレーム基準でOK)
  // -----------------------------------------------------------
  update() {
    const sp = this._speed();

    // swim ⇄ talk: pretalk/posttalk 中間フレームを介して切り替え (setState 側で transition を仕込む)。
    // talkRigActive が確定値、talkMix はそれの 0/1 表現。
    this.talkMix = this.talkRigActive ? 1 : 0;

    // 位相を進める (idle は「ひと休み」中だけ pose_cycle を据え置き = 腕を後ろに伸ばしたまま停止)
    let cycleWrapped = false;
    if (this.state === 'idle' && this.idleRestUntil > 0) {
      if (millis() >= this.idleRestUntil) {                 // ひと休みおわり → 再開 (次の休憩までの周数を引き直す)
        this.idleRestUntil = 0;
        this.idleCyclesDone = 0;
        this.idleSwimsUntilRest = Math.floor(random(IDLE_SWIMS_MIN, IDLE_SWIMS_MAX));
        // 復帰時、cyclePhase を p3→p4 segment の頭にジャンプ (手を後ろに戻さず前へ伸ばす方へ流す)。
        // droop の 0.5s fade と p3→p4 smoothstep がほぼ重なるので「だらーん→前へ」が滑らかに繋がる
        this.cyclePhase = this.idleRestExitPhase;
      }
      // 休憩中は cyclePhase を進めない (tailPhase 等は下で通常どおり進めるので体はゆらゆら漂う)
    } else {
      const prevPhase = this.cyclePhase;
      this.cyclePhase = (this.cyclePhase + (sp.cycle / RIG_CYCLE_FRAMES)) % 1;
      cycleWrapped = this.cyclePhase < prevPhase;
      if (this.state === 'idle') {
        if (cycleWrapped) this.idleCyclesDone++;                       // pose_cycle を 1 周した
        // そろそろ疲れた & 腕を後ろに伸ばした (p3 hold) ところに来た → ひと休み開始
        if (this.idleCyclesDone >= this.idleSwimsUntilRest
            && prevPhase < IDLE_REST_PHASE && this.cyclePhase >= IDLE_REST_PHASE) {
          this.cyclePhase   = IDLE_REST_PHASE;
          this.idleRestUntil = millis() + random(IDLE_REST_MIN_MS, IDLE_REST_MAX_MS);
        }
      }
    }
    // pretalk/posttalk: 時間経過で完了 (TALK_TRANSITION_MS)
    if (this.transitionState && millis() >= this.transitionEndAt) {
      if (this.transitionState === 'pretalk') this.talkRigActive = true;
      this.transitionState = null;
    }
    // だらーん係数: ひと休み中はじわっと増 → 動き出したらじわっと戻る
    const droopUp = (this.state === 'idle' && this.idleRestUntil > 0);
    this.idleDroop = Math.max(0, Math.min(1,
      this.idleDroop + (droopUp ? 1 / REST_DROOP_RAMP_FRAMES : -1 / REST_DROOP_RECOVER_FRAMES)));

    const swimWaveSpeed = this.rig.swim.spine.wave_speed;   // しなりの基準速度 (swim 側を採用)
    this.tailPhase += swimWaveSpeed * sp.wave;
    this.finPhase  += 0.11;
    this.bobPhase  += 0.02;

    // 横移動
    this.x += this.vx * this._velocityMul();
    // 壁判定は swim/talk の大きい方の canvas で固定する。dominantSet ごとに変えると
    // 状態遷移 (idle→speak 等) で「swim 壁内・talk 壁外」になり頭/手がはみ出す
    const halfW = Math.max(
      this.sets.swim.cfg.canvas_width  * this.sets.swim.scale * 0.5,
      this.sets.talk.cfg.canvas_width  * this.sets.talk.scale * 0.5);
    const halfH = Math.max(
      this.sets.swim.cfg.canvas_height * this.sets.swim.scale * 0.5,
      this.sets.talk.cfg.canvas_height * this.sets.talk.scale * 0.5);
    if (this.x < halfW && this.vx < 0)             this.vx = Math.abs(this.vx);
    if (this.x > width - halfW && this.vx > 0)     this.vx = -Math.abs(this.vx);

    // idle のみ縦移動: GuestFish 風に FISH_TURN_MIN/MAX_SEC 秒ごとに進行角を ±FISH_TILT_MAX_DEG 内で再抽選。
    // 休憩中 (idleRestUntil > 0) は再抽選しないが既存 vy で漂う (= ふわふわ浮く)。
    if (this.state === 'idle' && this.idleRestUntil <= 0 && millis() >= this.nextTurnAt) {
      const maxR = (FISH_TILT_MAX_DEG * Math.PI) / 180;
      const ang  = random(-maxR, maxR);
      const baseSpd = Math.max(0.35, Math.hypot(this.vx, this.vy));
      const dir  = this.vx >= 0 ? 1 : -1;        // 水平向きは維持 (壁反射に従う)
      this.vx = dir * baseSpd * Math.cos(ang);
      this.vy = baseSpd * Math.sin(ang);
      this.nextTurnAt = millis() + random(FISH_TURN_MIN_SEC, FISH_TURN_MAX_SEC) * 1000;
    }
    // vy にも _velocityMul を掛けてキック失速パターンを縦にも適用 (= 傾いた方向にキックで前進する)
    this.y += this.vy * this._velocityMul();
    if (this.y < halfH && this.vy < 0)             this.vy = Math.abs(this.vy);
    if (this.y > height - halfH && this.vy > 0)    this.vy = -Math.abs(this.vy);

    // 進行方向への傾き (idle で vy がある時のみ振れる、非 idle は vy=0 で自然に 0 へ収束)
    {
      const maxTilt = (FISH_TILT_MAX_DEG * Math.PI) / 180;
      let pitch = Math.atan2(this.vy, Math.abs(this.vx) + 0.0001);
      pitch = Math.max(-maxTilt, Math.min(maxTilt, pitch));
      const target = (this.vx >= 0) ? pitch : -pitch;
      this.tilt += (target - this.tilt) * FISH_TILT_LERP;
    }

    // 縦の微小ゆらぎ (leave は強調) — vy とは別に体の呼吸的なゆらぎ
    const bobAmp = (this.state === 'leave') ? 1.6 : 0.5;
    this.y += sin(this.bobPhase) * bobAmp;

    // 瞬き — swim/talk どちらでも、現在の dominant set に blink config があれば進める
    const domBlink = this.sets[this._dominantSet()].cfg.blink;
    if (domBlink) {
      const now = millis();
      if (this.blinkProgress < 0 && (now - this.lastBlinkAt) / 1000 > this.nextBlinkInterval) {
        this.blinkProgress = 0;
      }
      if (this.blinkProgress >= 0) {
        this.blinkProgress += 1 / Math.max(1, Math.round(domBlink.dur_ms / 33.3));
        if (this.blinkProgress > 1) {
          this.blinkProgress = -1;
          this.lastBlinkAt = now;
          this.nextBlinkInterval = random(domBlink.interval_min, domBlink.interval_max);
        }
      }
    }
  }

  // -----------------------------------------------------------
  // 描画
  // -----------------------------------------------------------
  draw() {
    // 話し中(speak)= talk セット / それ以外 = swim セット。即切り替え。
    this._drawSet(this.talkMix >= 0.5 ? 'talk' : 'swim');
  }

  _drawSet(key) {
    const set = this.sets[key];
    const cfg = set.cfg;
    push();
    translate(this.x, this.y);
    rotate(this.tilt);                       // idle 中の縦移動に応じて進行方向に傾ける (GuestFish と同様)
    const flip = this.vx > 0 ? -1 : 1;       // 元画像は左向き。右へ泳ぐときだけ反転
    scale(flip * set.scale, set.scale);
    translate(-set.anchor[0], -set.anchor[1]);

    // pose_cycle 上の現在位置 (limb ごとに phase をずらす)
    const cycleKeys = cfg.pose_cycle;
    for (const layer of cfg.layers) this._drawLayer(set, layer, cycleKeys);

    noTint();
    pop();
  }

  // local (0..1) が pose_cycle のどの区切りに入るか + その区切り内の進み具合 f (0..1) を返す
  _segAt(set, local) {
    const bk = set.breaks, n = bk.length - 1;
    let seg = n - 1;
    for (let i = 0; i < n; i++) { if (local < bk[i + 1]) { seg = i; break; } }
    const lo = bk[seg], hi = bk[seg + 1];
    const f = (hi > lo) ? Math.max(0, Math.min(1, (local - lo) / (hi - lo))) : 0;
    return [seg, f];
  }

  // --- 1 レイヤーを描く ---
  _drawLayer(set, layer, cycleKeys) {
    const cfg = set.cfg, imgs = set.imgs;
    switch (layer.type) {
      case 'spine':  this._drawSpine(set); break;
      case 'static': this._drawStatic(set, layer); break;
      case 'eye':    this._drawEye(set, layer); break;
      case 'mouth':  this._drawMouth(set); break;
      case 'limb':   this._drawLimb(set, layer, cycleKeys); break;
      default: break;
    }
  }

  // body: 縦スライスを sin 波でずらしてしならせる (初代と同じ式)
  _drawSpine(set) {
    const sp = set.cfg.spine;
    const img = set.imgs[sp.image];
    if (!img) return;
    const strips = sp.strips, span = sp.wave_span ?? 1.6;
    const sw0 = img.width / strips;
    for (let i = 0; i < strips; i++) {
      const t = i / (strips - 1);
      const amp = lerp(sp.wave_amp_head ?? 1, sp.wave_amp_tail ?? 16, t);
      const yOff = sin(this.tailPhase + t * PI * span) * amp;
      const sx = i * sw0;
      const sw = sw0 + 1;
      image(img, sx, yOff, sw, img.height, sx, 0, sw, img.height);
    }
  }

  // body のしなりが、与えた x にいる地点に作る縦オフセット (limb を体に貼り付けるのに使う)
  _waveYAt(set, x) {
    const sp = set.cfg.spine;
    const img = set.imgs[sp.image];
    if (!img) return 0;
    const strips = sp.strips, span = sp.wave_span ?? 1.6;
    let i = Math.floor(x / (img.width / strips));
    i = Math.max(0, Math.min(strips - 1, i));
    const t = i / (strips - 1);
    const amp = lerp(sp.wave_amp_head ?? 1, sp.wave_amp_tail ?? 16, t);
    return sin(this.tailPhase + t * PI * span) * amp;
  }

  _drawStatic(set, layer) {
    const img = set.imgs[layer.image];
    if (!img) return;
    if (layer.anim === 'flap' && layer.pivot) {
      const wy = this._waveYAt(set, layer.pivot[0]);
      const ang = sin(this.finPhase) * (layer.amp_deg ?? 6) * PI / 180;
      push();
      translate(0, wy);
      translate(layer.pivot[0], layer.pivot[1]);
      rotate(ang);
      translate(-layer.pivot[0], -layer.pivot[1]);
      image(img, 0, 0);
      pop();
    } else {
      image(img, 0, 0);
    }
  }

  _drawEye(set, layer) {
    const b = set.cfg.blink;
    let key = b.open;
    if (this.state === 'sleep') {
      key = b.closed;                                 // 寝ているときは常に閉じ目
    } else if (this.blinkProgress >= 0) {
      const p = this.blinkProgress;
      if (p >= 0.25 && p <= 0.75) key = b.closed;   // 中盤だけ閉じる
    }
    const img = set.imgs[key];
    if (!img) return;
    // anchor_x が指定されていれば body のしなりに同期 (目だけ画面上で固定気味だが微妙に動く)
    const wx = (layer && layer.anchor_x != null) ? layer.anchor_x : null;
    if (wx != null) {
      push();
      translate(0, this._waveYAt(set, wx));
      image(img, 0, 0);
      pop();
    } else {
      image(img, 0, 0);
    }
  }

  _drawMouth(set) {
    const m = set.cfg.mouth;
    let key = m.closed;
    if (this.state === 'speak') {
      const elapsed = millis() - this.speakStartedAt;
      const step = m.speak_step_ms ?? 130;
      key = (Math.floor(elapsed / step) % 2 === 0) ? m.closed : m.open;
    }
    const img = set.imgs[key];
    if (img) image(img, 0, 0);
  }

  // limb: 上腕/前腕 (腿/脛) の 2 ボーン階層回転
  _drawLimb(set, layer, cycleKeys) {
    const imgs = set.imgs;
    const up = imgs[layer.upper], lo = imgs[layer.lower];
    if (!up || !lo) return;

    // この limb の pose 角 [upperDeg, lowerDeg] を pose_cycle から求める (区切り長は set.breaks に従う)
    const ampMul = layer.amp_mul ?? 1;
    const local = ((this.cyclePhase + (layer.phase ?? 0)) % 1 + 1) % 1;
    const n = cycleKeys.length;
    let [seg, f] = this._segAt(set, local);
    if ((set.cfg.snap_from || []).includes(cycleKeys[seg])) {
      f = 1;                        // この区切りは『間を飛ばして一気に』— 区切りの頭で次ポーズへスナップ
    } else {
      // flow_through に入っている pose を「通り抜け」キーフレームとして扱う:
      //   そこに向かう区切り = ease_in (終わり全速 = キー手前で減速しない)
      //   そこから出る区切り = ease_out (始まり全速 = キー直後で加速しない)
      //   両端 flow_through = linear (定速で通過)
      // これで pose 列に「中継点」を作っても velocity 0 で停止せず流れる動きになる。
      const flow = set.cfg.flow_through || [];
      const endsAtFlow   = flow.includes(cycleKeys[(seg + 1) % n]);
      const startsAtFlow = flow.includes(cycleKeys[seg]);
      if (endsAtFlow && startsAtFlow)      { /* linear: f そのまま */ }
      else if (endsAtFlow)                 { f = f * f; }                          // ease_in
      else if (startsAtFlow)               { f = 1 - (1 - f) * (1 - f); }          // ease_out
      else                                 { f = f * f * (3 - 2 * f); }            // smoothstep (両端 ease)
    }
    const a = set.cfg.poses[cycleKeys[seg]][layer.name];
    const b = set.cfg.poses[cycleKeys[(seg + 1) % n]][layer.name];
    let uDeg = lerp(a[0], b[0], f) * ampMul;
    let lDeg = lerp(a[1], b[1], f) * ampMul;
    // pretalk/posttalk: swim rig のまま pose 角を縮めて talk 姿勢 (rest=全 0) に寄せる
    if (this.transitionState) {
      uDeg *= TALK_TRANSITION_POSE_SCALE;
      lDeg *= TALK_TRANSITION_POSE_SCALE;
    }
    // ひと休み中 (idle・swim) は手足がゆっくり下がる「だらーん」
    if (this.idleDroop > 0 && set === this.sets.swim) {
      const d = REST_DROOP[layer.name];
      if (d) { uDeg += d.u * this.idleDroop; lDeg += d.l * this.idleDroop; }
    }
    // 寝ているとき: pose を sleep に固定 (cycle/transition/droop に依らず上書き)
    if (this.state === 'sleep' && set.cfg.poses.sleep && set.cfg.poses.sleep[layer.name]) {
      uDeg = set.cfg.poses.sleep[layer.name][0] * ampMul;
      lDeg = set.cfg.poses.sleep[layer.name][1] * ampMul;
    }
    const uRad = uDeg * PI / 180, lRad = lDeg * PI / 180;

    const P = layer.proximal, D = layer.distal;
    const wy = (layer.anchor) ? this._waveYAt(set, P[0]) : 0;
    const off = layer.offset || [0, 0];           // limb 全体の平行移動 (付け根を fin の下に隠す等)
    const lowerOff = layer.lower_offset || [0, 0];// 下セグメント (脛+靴) だけの追加平行移動
    const lowerUnder = !!layer.lower_under;       // 脛+靴 を腿の下(裏)に描く (脚はこれ)
    const lowerFlipV = !!layer.lower_flip_v;      // 脛+靴 を distal 中心に天地反転して描く

    push();
    translate(off[0], off[1] + wy);
    // 上腕 (腿) を proximal 中心に uRad 回転 — このフレーム内に上下両セグメントを描く
    translate(P[0], P[1]); rotate(uRad); translate(-P[0], -P[1]);
    // 前腕+手 (脛+靴): 上腕の回転フレーム内で distal 中心に lRad 回転
    if (lowerUnder) this._drawLimbLower(lo, D, lRad, lowerFlipV, lowerOff);
    image(up, 0, 0);
    if (!lowerUnder) this._drawLimbLower(lo, D, lRad, lowerFlipV, lowerOff);
    pop();
  }

  _drawLimbLower(img, D, lRad, flipV, lowerOff) {
    push();
    translate(lowerOff[0], lowerOff[1]);                                   // 下セグメントだけの平行移動
    translate(D[0], D[1]); rotate(lRad); translate(-D[0], -D[1]);
    if (flipV) { translate(0, D[1]); scale(1, -1); translate(0, -D[1]); }  // distal の y を軸に天地反転
    image(img, 0, 0);
    pop();
  }
}

// =============================================================
// GuestFish クラス: お客さんがアップロードした 1 枚画像を泳がせる
//
// 既存 Fish の `_drawSpine` (縦スライス変形) を流用する単機能版。
// 状態機械なし、常時 swim。背景除去済みの透過 PNG が前提。
//
// 2026-05-11 改修:
//  1. 飼い主在席時の演出を「ハロー(淡い円)」→「拡大」に変更
//  2. 横だけでなく斜め上下にも泳ぐ (vy を実働 + 進行方向へ最大 ±15° の傾き)
//  3. 飼い主不在 / 未登録の魚は小さく表示 (small/big の 2 段サイズ、~0.4s で滑らかに補間)
//  4. 新規登録時は画面上端より上から落下 → 水面(y≈0)着水で「ぽちゃん」効果音 + 波紋
//     (落下〜着水中のサイズは飼い主在席サイズ=big で固定)
// =============================================================

// チューニング用定数
const GUEST_SIZE_SMALL   = 130;    // 飼い主不在 / 未登録のときの長辺 px
const GUEST_SIZE_BIG     = 210;    // 飼い主在席 / 新規登録の落下中のときの長辺 px (旧 260 → ~0.8x)
const GUEST_SIZE_LERP    = 0.12;   // 1 フレームのサイズ補間係数 (~0.4s で到達 @60fps)
const GUEST_TILT_MAX_DEG = 15;     // 進行方向への傾き上限 (兼: 上下動の角度上限)
const GUEST_TILT_LERP    = 0.12;   // 傾きの補間係数
const GUEST_TURN_MIN_SEC = 2.5;    // 進行角を変える間隔の下限
const GUEST_TURN_MAX_SEC = 6.0;    // 〃 上限
const GUEST_DROP_VY0     = 2.2;    // 落下開始時の下向き初速
const GUEST_DROP_GRAVITY = 0.42;   // 落下中の加速度
const GUEST_RIPPLE_SEC   = 0.85;   // 着水波紋の表示時間

// 飼い主に「気づいた!」リアクション (急拡大ではなく、しっぽ振りながらゆっくり近づいてくる感じ)
const GUEST_GROW_LERP_SLOW = 0.035; // 飼い主に気づいて拡大するときの補間 (~1.5s かけてゆっくり)
const GUEST_NOTICE_MS      = 1800;  // 「気づいたよ!」リアクションの継続時間
const GUEST_NOTICE_TAIL    = 2.3;   // リアクション中のしっぽの速さ倍率
const GUEST_NOTICE_SPEED   = 1.5;   // リアクション中の遊泳速度倍率 (近づいてくる感)
const GUEST_NOTICE_BOB     = 1.8;   // リアクション中の上下ゆらぎ倍率 (はしゃぐ感)

// 飼い主が現れた瞬間の「お祝いポップ」演出 (setOwnerPresent → celebrate())。
// dart(端へ) → approach(端→センターへ泳ぎながら拡大=水槽の奥から手前へ) →
// hold(センターで少し止まって魚っぽくしっぽを振る) → recede(センター→奥へ泳ぎながら A へ縮小)。
// 手前 (approach/hold/recede) の間だけ うおちゃんより前のレイヤーに描画する (isPopFront)。
// 速度は「泳いでいる感」重視でゆっくりめ。
const GUEST_POP_FRAC       = 1 / 4;  // 手前ピークサイズ = 画面の長辺 * これ (魚の長辺 px、1920で480px)
const GUEST_POP_COOLDOWN   = 10000;  // 同じ魚を再ポップさせない最小間隔 (ms)
const GUEST_POP_DART_MS    = 360;    // 端へダート
const GUEST_POP_APPROACH_MS = 1040;  // 端→センター、奥から手前へ来ながら拡大 (ゆっくり泳ぐ)
const GUEST_POP_HOLD_MS    = 800;    // センターで少し止まってしっぽを振る
const GUEST_POP_RECEDE_MS  = 1040;   // センター→奥へ去りながら A サイズに縮小 (ゆっくり泳ぐ)
const GUEST_POP_HOLD_TAIL  = 2.2;    // hold 中のしっぽ速度倍率 (魚っぽい振り)
const GUEST_POP_HOLD_AMP   = 1.5;    // hold 中のしっぽ振幅倍率
const GUEST_POP_SWIM_BOB   = 9;      // approach/recede の上下うねり幅 (px、泳いでる感)

// イージング (p5 に無いので簡易版)
function _easeOutCubic(p)  { const q = 1 - p; return 1 - q * q * q; }
function _easeInOutCubic(p) { return p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2; }

class GuestFish {
  constructor(image, options = {}) {
    this.image = image;                      // p5.Image (背景除去済)
    this.id = options.id || `guest_${Date.now()}_${Math.floor(Math.random() * 1e6)}`;

    // 表示サイズ: 長辺基準で small / big の 2 段。実描画スケールはその間を毎フレーム lerp。
    this._longest   = Math.max(image.width, image.height);
    this.smallScale = (options.smallSize || GUEST_SIZE_SMALL) / this._longest;
    this.bigScale   = (options.bigSize   || GUEST_SIZE_BIG)   / this._longest;

    // 飼い主登録
    this.ownerPersonId = options.ownerPersonId !== undefined ? options.ownerPersonId : null;
    this.isHighlighted = false;     // 飼い主が水槽前にいるとき true
    this.highlightStartedAt = 0;
    this.excitedUntil = 0;          // 飼い主に気づいた瞬間からこの時刻まで「気づいたよ!」リアクション

    // お祝いポップ演出の状態 (celebrate() で起動)
    this.popPhase        = null;    // null | 'dart' | 'approach' | 'hold' | 'recede'
    this.popStartedAt    = 0;       // 現フェーズの開始 millis()
    this._lastCelebrateAt = -1e9;   // 最後にポップした millis() (クールダウン判定)
    this._tailAmpMul     = 1;       // しっぽ振幅倍率 (将来用、通常 1)
    this.popHomeX = 0; this.popHomeY = 0;        // ポップ開始時の元位置 (recede の戻り先)
    this.dartX = 0;   this.dartY = 0;            // ダート先 (近い側の端)
    this.dartFromRight = false;                  // 右端へダートするか
    this.popCenterX = 0; this.popCenterY = 0;    // 手前ピークの位置 (画面センター)
    this.popBaseScale = 0;          // approach 起点スケール
    this.popScaleTarget = 0;        // 手前ピークの目標スケール (画面1/4)

    // 入場演出:
    //  - dropIn:       今日新規登録された魚。上からドロップイン + 着水音 + その日ずっと big。
    //  - startupEntry: 起動時に既存魚を 1 匹ずつ時間差で「上からドロップイン」させるとき。
    //                  音なし、サイズ・レイヤーは通常 (飼い主不在なら small)。
    this.dropIn       = !!options.dropIn;
    this.startupEntry = !!options.startupEntry;
    this.entering     = this.dropIn || this.startupEntry;
    this.entryPhase   = this.entering ? 'falling' : 'done';   // falling -> sinking -> done
    this.onSplash     = typeof options.onSplash === 'function' ? options.onSplash : null;
    this.settleY      = 0;
    this.splashedAt   = 0;
    this.splashX      = 0;

    // 初期位置・速度
    if (this.entering) {
      this.x  = options.x !== undefined ? options.x : random(width * 0.15, width * 0.85);
      // 入場開始時のサイズ: 新規登録は big、起動時の既存魚は通常 (= 飼い主不在なら small)
      this.scale = this.dropIn ? this.bigScale : this.smallScale;
      this.y  = -this._longest * this.scale * 0.7;            // 画面上端より上から落とす
      this.vx = (random() < 0.5 ? -1 : 1) * random(0.35, 0.7);  // 着水後に使う水平速度
      this.vy = GUEST_DROP_VY0;
    } else {
      this.x  = options.x !== undefined ? options.x : random(width * 0.2, width * 0.8);
      this.y  = options.y !== undefined ? options.y : random(height * 0.25, height * 0.7);
      const speed = options.speed || random(0.4, 0.85);
      this.vx = (random() < 0.5 ? -1 : 1) * speed;
      this.vy = 0;
      this.scale = this.smallScale;
    }

    // 見た目の傾き (rad)。進行方向へ ±GUEST_TILT_MAX_DEG。lerp で滑らかに。
    this.tilt   = 0;
    this.facing = this.vx >= 0 ? 1 : -1;   // 1=右向き / -1=左向き (画像は左向き前提)

    // 進行角を変えるタイマー
    this.nextTurnAt = millis() + random(GUEST_TURN_MIN_SEC, GUEST_TURN_MAX_SEC) * 1000;

    // アニメ位相 (個体差のため初期値ランダム)
    this.tailPhase = random(TWO_PI);
    this.bobPhase  = random(TWO_PI);

    // スライス設定 (Fish._drawSpine と同等)。waveAmpTail / waveSpeed は個体差を出すため
    // 1 匹ずつランダム化 (位相だけでなく振り幅・速さも変えて「みんな揃って見える」のを防ぐ)。
    this.strips      = options.strips      || 22;
    this.waveAmpHead = options.waveAmpHead || 1;
    this.waveAmpTail = options.waveAmpTail || random(11, 17);
    this.waveSpeed   = options.waveSpeed   || random(0.13, 0.20);
  }

  // -----------------------------------------------------------
  // 飼い主登録 / 在席演出
  // -----------------------------------------------------------
  setOwnerPersonId(pid) {
    this.ownerPersonId = pid || null;
  }

  setHighlighted(on) {
    if (on === this.isHighlighted) return;
    this.isHighlighted = !!on;
    if (this.isHighlighted) {
      this.highlightStartedAt = millis();
      this.excitedUntil = millis() + GUEST_NOTICE_MS;   // 「気づいたよ!」リアクション開始
    }
  }

  // クールダウン中 (直近 GUEST_POP_COOLDOWN 以内) かどうか。sketch 側の抽選で
  // 「ポップできる魚」だけを候補にするために参照する。
  canCelebrate() {
    return !this.entering && this.popPhase === null
        && (millis() - this._lastCelebrateAt) >= GUEST_POP_COOLDOWN;
  }

  // 飼い主が現れた瞬間の「お祝いポップ」を起動する (sketch 側で 1 匹だけ選んで呼ぶ)。
  // dart(端へ) → approach(奥→手前、拡大) → recede(手前→奥、A へ縮小) の 3 フェーズ。
  celebrate() {
    if (!this.canCelebrate()) return false;
    this._lastCelebrateAt = millis();
    this.popPhase     = 'dart';
    this.popStartedAt = millis();
    this.popHomeX     = this.x;
    this.popHomeY     = this.y;
    this.popBaseScale = this.scale;

    // ダート先: 近い側の横端 (まだ小さいので現在サイズで見切れない位置)
    const edgeMargin = Math.max(40, this._longest * this.scale * 0.5);
    this.dartFromRight = this.x > width / 2;
    this.dartX = this.dartFromRight ? (width - edgeMargin) : edgeMargin;
    this.dartY = this.y;

    // 手前ピーク: 画面の長辺 1/4 のサイズで画面センターに来る
    const peakLong = Math.max(width, height) * GUEST_POP_FRAC;
    this.popScaleTarget = peakLong / this._longest;
    this.popCenterX = width / 2;
    this.popCenterY = height / 2;
    return true;
  }

  // 手前 (うおちゃんより前のレイヤー) に描くべきか。approach/hold/recede の間 true。
  isPopFront() {
    return this.popPhase === 'approach' || this.popPhase === 'hold' || this.popPhase === 'recede';
  }

  // 「前面(大)グループ」に入るか:
  //  - 今日新しく入った魚 (dropIn) … その日はずっと前(大)のまま (ページ再読込=翌日の電源 ON でリセット)
  //  - 飼い主が水槽前にいる (isHighlighted)
  // それ以外は「奥(小)グループ」。起動時の既存魚のドロップイン (startupEntry) は通常サイズ・通常レイヤー。
  isBig() {
    return this.dropIn || this.isHighlighted;
  }

  // いま目指す表示スケール
  _targetScale() {
    return this.isBig() ? this.bigScale : this.smallScale;
  }

  // 進行方向に応じた目標傾き (rad)。
  //  右へ進む(facing=1, 反転して右向き): 下向き(vy>0)に傾けるには +pitch
  //  左へ進む(facing=-1, 反転なし):       下向きに傾けるには -pitch
  _targetTilt() {
    const maxR = (GUEST_TILT_MAX_DEG * Math.PI) / 180;
    let pitch = Math.atan2(this.vy, Math.abs(this.vx) + 0.0001);
    pitch = Math.max(-maxR, Math.min(maxR, pitch));
    return (this.facing >= 0) ? pitch : -pitch;
  }

  update() {
    // お祝いポップ中は専用の振り付けが位置・スケール・しっぽを支配する (通常遊泳は止める)
    if (this.popPhase !== null) {
      this._updatePop();
      this.tilt += (this._targetTilt() - this.tilt) * GUEST_TILT_LERP;
      return;
    }

    const exciting = millis() < this.excitedUntil;   // 「気づいたよ!」リアクション中か

    // しっぽ・呼吸ゆらぎの位相 (リアクション中は速く＝はしゃぐ感)
    this.tailPhase += this.waveSpeed * (exciting ? GUEST_NOTICE_TAIL : 1.0);
    this.bobPhase  += 0.02 * (exciting ? 1.7 : 1.0);

    if (this.entering) this._updateEntering();
    else               this._updateSwim(exciting);

    // 表示スケール: 飼い主に気づいて大きくなる途中だけゆっくり (急拡大しない)、それ以外は通常速度
    const target = this._targetScale();
    const noticingGrow = this.isHighlighted && !this.entering && (target - this.scale) > 0.0001;
    const sizeLerp = noticingGrow ? GUEST_GROW_LERP_SLOW : GUEST_SIZE_LERP;
    this.scale += (target - this.scale) * sizeLerp;

    this.tilt += (this._targetTilt() - this.tilt) * GUEST_TILT_LERP;
  }

  // お祝いポップの 4 フェーズ。millis() ベースで進行し、recede 完了で popPhase=null に戻す。
  _updatePop() {
    const t = millis() - this.popStartedAt;
    this._tailAmpMul = 1;

    if (this.popPhase === 'dart') {
      // 端へダート。サイズはまだ元のまま (奥にいる)。
      const p = Math.min(1, t / GUEST_POP_DART_MS);
      const e = _easeOutCubic(p);
      this.x = lerp(this.popHomeX, this.dartX, e);
      this.y = lerp(this.popHomeY, this.dartY, e);
      this.facing = this.dartFromRight ? 1 : -1;
      this.tailPhase += this.waveSpeed * 1.8;
      if (p >= 1) { this.popPhase = 'approach'; this.popStartedAt = millis(); }

    } else if (this.popPhase === 'approach') {
      // 端 → センターへ泳ぎながら拡大 (水槽の奥から手前へ来る)。手前レイヤーで描画。
      // 上下にうねらせて「泳いでる感」を出す (中央に着くにつれ収束)。
      const p = Math.min(1, t / GUEST_POP_APPROACH_MS);
      const e = _easeInOutCubic(p);
      this.bobPhase += 0.12;
      const bob = Math.sin(this.bobPhase) * GUEST_POP_SWIM_BOB * (1 - e);
      this.x = lerp(this.dartX, this.popCenterX, e);
      this.y = lerp(this.dartY, this.popCenterY, e) + bob;
      this.scale = lerp(this.popBaseScale, this.popScaleTarget, e);
      this.facing = (this.popCenterX >= this.dartX) ? 1 : -1;
      this.tailPhase += this.waveSpeed * 1.6;
      if (p >= 1) { this.scale = this.popScaleTarget; this.popPhase = 'hold'; this.popStartedAt = millis(); }

    } else if (this.popPhase === 'hold') {
      // センターで少し止まって、魚っぽくしっぽを振る (速度↑・振幅↑)。軽く上下に呼吸。
      const p = Math.min(1, t / GUEST_POP_HOLD_MS);
      this.scale = this.popScaleTarget;
      this.bobPhase += 0.07;
      this.x = this.popCenterX;
      this.y = this.popCenterY + Math.sin(this.bobPhase) * 4;
      this.tailPhase += this.waveSpeed * GUEST_POP_HOLD_TAIL;
      this._tailAmpMul = GUEST_POP_HOLD_AMP;
      if (p >= 1) { this.popPhase = 'recede'; this.popStartedAt = millis(); }

    } else if (this.popPhase === 'recede') {
      // センター → 奥 (元位置) へ泳ぎながら A サイズへ縮小 (手前から奥へ去る)。
      const p = Math.min(1, t / GUEST_POP_RECEDE_MS);
      const e = _easeInOutCubic(p);
      this.bobPhase += 0.12;
      const bob = Math.sin(this.bobPhase) * GUEST_POP_SWIM_BOB * (1 - e);
      this.scale = lerp(this.popScaleTarget, this._targetScale(), e);
      this.x = lerp(this.popCenterX, this.popHomeX, e);
      this.y = lerp(this.popCenterY, this.popHomeY, e) + bob;
      this.facing = (this.popHomeX >= this.popCenterX) ? 1 : -1;
      this.tailPhase += this.waveSpeed * 1.6;
      if (p >= 1) {
        this.popPhase = null;
        this.scale = this._targetScale();
        this.x = this.popHomeX;
        this.y = this.popHomeY;
        this.facing = this.vx >= 0 ? 1 : -1;   // 通常遊泳の向きへ戻す
      }
    }
  }

  _updateEntering() {
    if (this.entryPhase === 'falling') {
      this.vy += GUEST_DROP_GRAVITY;
      this.y  += this.vy;
      if (this.y >= 0) {
        // 水面に着水
        this.entryPhase = 'sinking';
        this.splashedAt = millis();
        this.splashX    = this.x;
        if (this.onSplash) { try { this.onSplash(); } catch (e) { /* ignore */ } this.onSplash = null; }
        this.settleY = random(height * 0.18, height * 0.55);  // 沈み込み先
        this.vy *= 0.55;                                       // 水の抵抗で少し減速
      }
    } else if (this.entryPhase === 'sinking') {
      // settleY へバネ + 減衰 (少し沈んでから浮き上がる感じ)
      this.vy += (this.settleY - this.y) * 0.018;
      this.vy *= 0.90;
      this.y  += this.vy;
      if (Math.abs(this.y - this.settleY) < 6 && Math.abs(this.vy) < 0.4) {
        this.entering   = false;
        this.entryPhase = 'done';
        this.vy = 0;
        this.facing = this.vx >= 0 ? 1 : -1;
        this.nextTurnAt = millis() + random(GUEST_TURN_MIN_SEC, GUEST_TURN_MAX_SEC) * 1000;
      }
    }
  }

  _updateSwim(exciting = false) {
    // たまに進行角を変える (水平の向きは維持、上下成分を ±GUEST_TILT_MAX_DEG の範囲で入れる)
    if (millis() >= this.nextTurnAt) {
      const maxR = (GUEST_TILT_MAX_DEG * Math.PI) / 180;
      const ang  = random(-maxR, maxR);
      const spd  = Math.max(0.35, Math.hypot(this.vx, this.vy));
      this.vx = this.facing * spd * Math.cos(ang);
      this.vy = spd * Math.sin(ang);
      this.nextTurnAt = millis() + random(GUEST_TURN_MIN_SEC, GUEST_TURN_MAX_SEC) * 1000;
    }

    // 「気づいたよ!」リアクション中は速く動き (近づいてくる感)、上下のゆらぎも大きく (はしゃぐ感)
    const spdMul = exciting ? GUEST_NOTICE_SPEED : 1.0;
    const bobMul = exciting ? GUEST_NOTICE_BOB   : 1.0;
    this.x += this.vx * spdMul;
    this.y += this.vy * spdMul + sin(this.bobPhase) * 0.25 * bobMul;

    // 壁で反射
    const half = this._longest * this.scale * 0.5;
    if (this.x < half && this.vx < 0)            { this.vx = Math.abs(this.vx);  this.facing = 1; }
    if (this.x > width - half && this.vx > 0)    { this.vx = -Math.abs(this.vx); this.facing = -1; }
    if (this.y < half && this.vy < 0)            { this.vy = Math.abs(this.vy); }
    if (this.y > height - half && this.vy > 0)   { this.vy = -Math.abs(this.vy); }
  }

  draw() {
    // ── 着水波紋 (水面 y≈0 のあたりに広がる扁平リング) ──
    if (this.splashedAt > 0) {
      const dt = (millis() - this.splashedAt) / 1000;
      if (dt >= 0 && dt < GUEST_RIPPLE_SEC) {
        const p    = dt / GUEST_RIPPLE_SEC;
        const rMax = this._longest * this.bigScale * 0.9;
        const r    = lerp(10, rMax, p);
        const a    = (1 - p) * 130;
        push();
        noFill();
        stroke(255, 245, 205, a);
        strokeWeight(3);
        ellipse(this.splashX, 3, r * 2, r * 0.5);
        strokeWeight(1.5);
        ellipse(this.splashX, 3, r * 1.25, r * 0.32);
        pop();
      } else if (dt >= GUEST_RIPPLE_SEC) {
        this.splashedAt = 0;   // 一度だけ
      }
    }

    push();
    translate(this.x, this.y);

    // 進行方向へ軽く傾ける → そのあと水平反転 + スケール (順序的に flip は無回転フレームで適用される)
    rotate(this.tilt);

    // 画像は「左向き」前提。右向き(facing=1)のときだけ水平反転する。
    const flip = this.facing >= 0 ? -1 : 1;
    scale(flip * this.scale, this.scale);

    // 画像中心を fish の位置に揃える
    translate(-this.image.width / 2, -this.image.height / 2);

    // 縦スライス変形 (Fish._drawSpine と同じ式)
    const stripW = this.image.width / this.strips;
    for (let i = 0; i < this.strips; i++) {
      const t = i / (this.strips - 1);
      const amp = lerp(this.waveAmpHead, this.waveAmpTail, t) * this._tailAmpMul;
      const yOff = sin(this.tailPhase + t * PI * 1.6) * amp;
      const sx = i * stripW;
      const sw = stripW + 1;
      image(this.image, sx, yOff, sw, this.image.height, sx, 0, sw, this.image.height);
    }

    pop();
  }
}
