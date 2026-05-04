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
