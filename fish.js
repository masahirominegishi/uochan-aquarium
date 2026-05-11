// =============================================================
// Fish クラス: パーツ別 PNG をリギングして合成描画する
//
// 旧方式 (1 枚画像を縦スライス変形) からパーツ方式に移行。
// 各パーツは 800×800 透過 PNG。配置・回転中心・アニメ種別は
// `assets/uochan/parts.json` で定義。
//
// アニメ種別:
//   spine    : 縦スライス変形 (body 用、しなり泳ぎ)
//   follow   : 親に追従するゆるい上下動 (head)
//   swing    : pivot を中心に sin 波で回転 (arm, leg)
//   frames   : 状態に応じてフレーム PNG を切替 (mouth, eye)
//
// 状態:
//   idle / approach / speak / leave
//   - speak のとき mouth が closed/half/open/half ループ
//   - eye はランダム間隔で瞬き
// =============================================================

class Fish {
  constructor(parts, config) {
    this.parts  = parts;          // { imageName: p5.Image }
    this.config = config;         // parts.json の中身

    // 表示スケール (canvas の何倍に縮小して画面に出すか)
    this.scale = (config.display_width || 280) / config.canvas_width;

    // 位置・速度
    this.x = width * 0.5;
    this.y = height * 0.5;
    this.vx = -0.6;
    this.vy = 0;

    // アニメ位相
    this.tailPhase = 0;
    this.bobPhase  = random(TWO_PI);
    this.swingPhase = 0;          // 手足の共通位相 (state ごとに進む速度を変える)

    // 状態
    this.state = 'idle';
    this.stateChangedAt = millis();
    this.speakStartedAt = 0;

    // 瞬き
    this.lastBlinkAt = millis();
    this.nextBlinkInterval = this._randomBlinkInterval();
    this.blinkProgress = -1;  // -1 = 開いてる, 0..1 = 瞬き中

    // パーツを z 順にソート (1 回だけ)
    this.sortedParts = [...config.parts].sort((a, b) => a.z - b.z);
  }

  // -----------------------------------------------------------
  // 状態切替 (ws-client.js から呼ばれる)
  // -----------------------------------------------------------
  setState(newState) {
    if (this.state === newState) return;
    this.state = newState;
    this.stateChangedAt = millis();
    if (newState === 'speak') this.speakStartedAt = millis();
    console.log(`[Fish] state -> ${newState}`);
  }

  // -----------------------------------------------------------
  // フレーム更新
  // -----------------------------------------------------------
  update() {
    this.tailPhase += this._waveSpeed();
    this.bobPhase  += 0.02;
    const swingMul = this._swingMultiplier();
    this.swingPhase += 0.18 * swingMul.speed;

    // 横移動 (idle は平泳ぎ的にパルス: 腕の前→後 (引き動作) でピーク 3x、後→前 (戻し) でゆっくり)
    let velocityMul = 1.0;
    if (this.state === 'idle') {
      // arm_l: 下伸び phase=0 / arm_r: 上伸び phase=π にしてあるので
      // sin(swingPhase) = +1 のとき両腕が前 (画面左) を指す。
      // d(angle)/dt = cos(swingPhase): c < 0 のとき腕は前→後ろへ動く (推進) / c > 0 で戻し
      const c = cos(this.swingPhase);
      // c=-1 (推進ピーク): 3.0 倍 / c=+1 (戻しピーク): 0.15 倍 / c=0 (中間): ~1.5
      velocityMul = 1.575 - c * 1.425;
    } else if (this.state === 'speak') {
      velocityMul = 0.1;        // ほぼ停止して話す
    } else if (this.state === 'approach') {
      velocityMul = 1.6;
    } else if (this.state === 'leave') {
      velocityMul = 2.2;        // 一気に去る
    }
    this.x += this.vx * velocityMul;
    const halfW = this.config.canvas_width * this.scale * 0.5;
    if (this.x < halfW && this.vx < 0)               this.vx = Math.abs(this.vx);
    if (this.x > width - halfW && this.vx > 0)       this.vx = -Math.abs(this.vx);

    // 縦のゆらぎ (leave は深く沈むような上下動を強調)
    const bobAmp = (this.state === 'leave') ? 1.6 : 0.5;
    this.y += sin(this.bobPhase) * bobAmp;

    // 瞬き判定
    const now = millis();
    if (this.blinkProgress < 0 && (now - this.lastBlinkAt) / 1000 > this.nextBlinkInterval) {
      this.blinkProgress = 0;
    }
    if (this.blinkProgress >= 0) {
      this.blinkProgress += 1 / 18;  // 約 0.3 秒で瞬き完了 (60fps 換算)
      if (this.blinkProgress > 1) {
        this.blinkProgress = -1;
        this.lastBlinkAt = now;
        this.nextBlinkInterval = this._randomBlinkInterval();
      }
    }
  }

  _waveSpeed() {
    switch (this.state) {
      case 'approach': return 0.30;
      case 'speak':    return 0.10;
      case 'leave':    return 0.34;
      default:         return 0.16;  // idle: ゆったりしなり
    }
  }

  // state ごとの手足スイングの増幅とスピード
  _swingMultiplier() {
    switch (this.state) {
      case 'approach': return { amp: 1.1, speed: 1.5 };
      case 'speak':    return { amp: 0.4, speed: 0.4 };
      case 'leave':    return { amp: 1.4, speed: 1.7 };
      case 'idle':
      default:         return { amp: 1.7, speed: 0.7 };  // 大きくゆっくり
    }
  }

  _randomBlinkInterval() {
    return random(3.0, 7.0);
  }

  // -----------------------------------------------------------
  // 描画
  // -----------------------------------------------------------
  draw() {
    push();
    translate(this.x, this.y);

    // 進行方向で水平反転 (元画像は左向きなので vx<=0 はそのまま)
    const flip = this.vx > 0 ? -1 : 1;
    scale(flip * this.scale, this.scale);

    // 800x800 キャンバスの中心を fish の位置に揃える
    translate(-this.config.canvas_width / 2, -this.config.canvas_height / 2);

    // z 順にパーツ描画
    for (const part of this.sortedParts) {
      this._drawPart(part);
    }
    pop();
  }

  _drawPart(part) {
    switch (part.anim) {
      case 'spine':  this._drawSpine(part); break;
      case 'follow': this._drawFollow(part); break;
      case 'swing':  this._drawSwing(part); break;
      case 'frames': this._drawFrames(part); break;
      default:       this._drawStatic(part); break;
    }
  }

  _drawStatic(part) {
    const img = this.parts[part.image];
    if (img) image(img, 0, 0);
  }

  // body 用: 縦スライスを sin 波でずらして「しなり」を作る
  _drawSpine(part) {
    const img = this.parts[part.image];
    if (!img) return;
    const strips = 22;
    const sStripW = img.width / strips;
    for (let i = 0; i < strips; i++) {
      const t = i / (strips - 1);
      const amp = lerp(part.wave_amp_head ?? 1, part.wave_amp_tail ?? 16, t);
      const yOff = sin(this.tailPhase + t * PI * 1.6) * amp;
      const sx = i * sStripW;
      const sw = sStripW + 1;
      image(img, sx, yOff, sw, img.height, sx, 0, sw, img.height);
    }
  }

  // head 用: 体の bob にうっすら追従する控えめな上下動
  _drawFollow(part) {
    const img = this.parts[part.image];
    if (!img) return;
    const amp = part.follow_amp ?? 4;
    const yOff = sin(this.bobPhase) * amp;
    image(img, 0, yOff);
  }

  // 手足用: pivot 中心に sin 波で回転 (state ごとに amp が変わる)
  _drawSwing(part) {
    const img = this.parts[part.image];
    if (!img) return;
    const baseAmpDeg = part.swing_amp_deg ?? 20;
    const phase = part.phase ?? 0;
    const mul = this._swingMultiplier();
    const angle = sin(this.swingPhase + phase) * (baseAmpDeg * mul.amp * PI / 180);

    push();
    translate(part.pivot[0], part.pivot[1]);
    rotate(angle);
    translate(-part.pivot[0], -part.pivot[1]);
    image(img, 0, 0);
    pop();
  }

  // mouth/eye 用: 状態に応じてフレーム PNG を選択
  _drawFrames(part) {
    let frameKey = part.default;

    if (part.name === 'mouth' && this.state === 'speak') {
      const elapsed = millis() - this.speakStartedAt;
      const stepMs  = part.speak_step_ms ?? 150;
      const cycle   = part.speak_cycle ?? [part.default];
      frameKey = cycle[Math.floor(elapsed / stepMs) % cycle.length];
    }

    if (part.name === 'eye' && this.blinkProgress >= 0) {
      const p = this.blinkProgress;
      if (p < 0.35)      frameKey = 'half';
      else if (p < 0.65) frameKey = 'closed';
      else               frameKey = 'half';
    }

    const imgName = (part.frames && part.frames[frameKey]) || part.image;
    const img = this.parts[imgName];
    if (img) image(img, 0, 0);
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
const GUEST_SIZE_BIG     = 260;    // 飼い主在席 / 落下中のときの長辺 px
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

    // ドロップイン (新規登録時のみ true)。落下〜着水中はサイズ big 固定。
    this.dropIn     = !!options.dropIn;
    this.entering   = this.dropIn;
    this.entryPhase = this.dropIn ? 'falling' : 'done';   // falling -> sinking -> done
    this.onSplash   = typeof options.onSplash === 'function' ? options.onSplash : null;
    this.settleY    = 0;
    this.splashedAt = 0;
    this.splashX    = 0;

    // 初期位置・速度
    if (this.dropIn) {
      this.x  = options.x !== undefined ? options.x : random(width * 0.25, width * 0.75);
      this.y  = -this._longest * this.bigScale * 0.7;   // 画面上端より上
      this.vx = (random() < 0.5 ? -1 : 1) * random(0.35, 0.7);  // 着水後に使う水平速度
      this.vy = GUEST_DROP_VY0;
      this.scale = this.bigScale;
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

    // スライス設定 (Fish._drawSpine と同等)
    this.strips      = options.strips      || 22;
    this.waveAmpHead = options.waveAmpHead || 1;
    this.waveAmpTail = options.waveAmpTail || 14;
    this.waveSpeed   = options.waveSpeed   || 0.16;
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

  // 「前面(大)グループ」に入るか:
  //  - 落下〜着水中 (entering)
  //  - 今日新しく入った魚 (dropIn) … その日はずっと前のまま (ページ再読込=翌日の電源 ON でリセット)
  //  - 飼い主が水槽前にいる (isHighlighted)
  // それ以外は「奥(小)グループ」。
  isBig() {
    return this.entering || this.dropIn || this.isHighlighted;
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
      const amp = lerp(this.waveAmpHead, this.waveAmpTail, t);
      const yOff = sin(this.tailPhase + t * PI * 1.6) * amp;
      const sx = i * stripW;
      const sw = stripW + 1;
      image(this.image, sx, yOff, sw, this.image.height, sx, 0, sw, this.image.height);
    }

    pop();
  }
}
