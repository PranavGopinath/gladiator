'use strict';
/* Pixel coliseum: a Phaser scene driven entirely by referee state from dashboard.js.
   It renders facts (who stands, who fell, who the kernel says did it, Jev's threat
   read) as a top-down 8-bit bout. It never decides anything itself. */
(function () {
  const GW = 840, GH = 470, CX = GW / 2, CY = GH / 2 + 8, RX = 330, RY = 196;
  const PX = '"Press Start 2P", monospace';
  const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const hex = c => parseInt(String(c).replace('#', ''), 16);
  const threatColor = v => v > .6 ? 0x39e08a : v > .3 ? 0xffd23f : 0xff5b6e;

  function fighterCanvas(color, ko) {
    const c = document.createElement('canvas'); c.width = 16; c.height = 16; const x = c.getContext('2d');
    const dark = 'rgba(0,0,0,.4)', steel = '#cfd8e6', light = 'rgba(255,255,255,.35)';
    if (ko) {
      x.fillStyle = '#3a4057'; x.fillRect(4, 5, 8, 7);
      x.fillStyle = '#262b3c'; x.fillRect(3, 8, 3, 3); x.fillRect(11, 6, 2, 4);
      x.fillStyle = '#8a93ad'; x.fillRect(6, 6, 4, 4); x.fillStyle = '#1a1d28'; x.fillRect(7, 7, 1, 1); x.fillRect(9, 7, 1, 1);
      return c;
    }
    x.fillStyle = dark; x.fillRect(4, 5, 9, 8);
    x.fillStyle = color; x.fillRect(4, 4, 8, 8);
    x.fillStyle = light; x.fillRect(4, 4, 8, 2);
    x.fillStyle = color; x.fillRect(3, 5, 2, 5); x.fillRect(11, 5, 2, 5);
    x.fillStyle = dark; x.fillRect(3, 5, 1, 5);
    x.fillStyle = '#e9eef7'; x.fillRect(6, 6, 4, 4);
    x.fillStyle = color; x.fillRect(7, 7, 2, 2);
    x.fillStyle = steel; x.fillRect(13, 7, 4, 2);
    x.fillStyle = '#ffd23f'; x.fillRect(12, 6, 2, 4);
    x.fillStyle = dark; x.fillRect(2, 6, 2, 4);
    return c;
  }

  function stations(count) {
    const pts = [];
    if (count === 2) return [[CX - 150, CY], [CX + 150, CY]];
    if (count === 3) return [[CX, CY - 110], [CX - 160, CY + 70], [CX + 160, CY + 70]];
    for (let i = 0; i < count; i++) {
      const a = -Math.PI / 2 + i * 2 * Math.PI / count;
      pts.push([CX + Math.cos(a) * 190, CY + Math.sin(a) * 118]);
    }
    return pts;
  }

  class Scene extends Phaser.Scene {
    constructor() { super('arena'); this.fighters = {}; this.roster = ''; this.pending = []; this.selectCb = null; }
    preload() {
      const p = document.createElement('canvas'); p.width = 4; p.height = 4; const px = p.getContext('2d'); px.fillStyle = '#fff'; px.fillRect(0, 0, 4, 4);
      this.textures.addCanvas('spark', p);
    }
    create() {
      const g = this.add.graphics();
      g.fillStyle(0x05050d, 1).fillRect(0, 0, GW, GH);
      let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
      const pal = [0x3a3d5c, 0x4a3a66, 0x5c3a4a, 0x3a4a5c, 0x55506a];
      for (let k = 0; k < 420; k++) {
        const ang = rnd() * Math.PI * 2, rr = 1.04 + rnd() * 0.17;
        g.fillStyle(pal[k % pal.length], 0.85).fillRect(CX + Math.cos(ang) * RX * rr, CY + Math.sin(ang) * RY * rr, 3, 3);
      }
      g.fillStyle(0x15152b, 1).fillEllipse(CX, CY, RX * 2 + 34, RY * 2 + 34);
      g.fillStyle(0x0c0c1c, 1).fillEllipse(CX, CY, RX * 2 + 10, RY * 2 + 10);
      g.fillStyle(0x161032, 1).fillEllipse(CX, CY, RX * 2, RY * 2);
      g.fillStyle(0x1d1540, 0.6).fillEllipse(CX, CY, RX * 2 - 40, RY * 2 - 40);
      g.lineStyle(2, 0x2d2a5e, 0.9);
      [1, 0.7, 0.42, 0.16].forEach(f => g.strokeEllipse(CX, CY, RX * 2 * f, RY * 2 * f));
      g.lineStyle(1, 0x2d2a5e, 0.5); g.lineBetween(CX - RX, CY, CX + RX, CY); g.lineBetween(CX, CY - RY, CX, CY + RY);
      [[CX, CY - RY], [CX, CY + RY], [CX - RX, CY], [CX + RX, CY]].forEach(([gx, gy]) => {
        g.fillStyle(0x23203f, 1).fillRect(gx - 7, gy - 7, 14, 14); g.fillStyle(0xffd23f, 0.25).fillRect(gx - 3, gy - 3, 6, 6);
      });
      this.add.text(CX, CY, '⚔', { fontFamily: PX, fontSize: '26px', color: '#2a2650' }).setOrigin(.5).setAlpha(.6);
      this.idle = this.add.text(CX, CY + 60, 'AWAITING CONTENDERS', { fontFamily: PX, fontSize: '9px', color: '#5b6788' }).setOrigin(.5);
      this.layer = this.add.container(0, 0);
      this.emitter = this.add.particles(0, 0, 'spark', { speed: { min: 50, max: 160 }, scale: { start: 1.6, end: 0 }, lifespan: 520, quantity: 0, tint: [0xffd23f, 0xff5b6e, 0xffffff], emitting: false });
      this.crownText = this.add.text(0, 0, '👑', { fontSize: '15px' }).setOrigin(.5).setVisible(false);
      this.readyFlag = true;
      this.pending.splice(0).forEach(fn => fn());
    }
    whenReady(fn) { this.readyFlag ? fn() : this.pending.push(fn); }

    setRoster(players) {
      const key = players.map(p => p.id).join('|');
      if (key !== this.roster) {
        this.roster = key;
        Object.values(this.fighters).forEach(f => f.group.destroy(true));
        this.fighters = {};
        this.idle.setVisible(!players.length);
        const spots = stations(players.length);
        players.forEach((p, i) => {
          const [sx, sy] = spots[i].map((v, k) => k ? v + 10 : v);
          const ok = 'f_' + p.id, ko = 'ko_' + p.id;
          if (this.textures.exists(ok)) this.textures.remove(ok);
          if (this.textures.exists(ko)) this.textures.remove(ko);
          this.textures.addCanvas(ok, fighterCanvas(p.color, false));
          this.textures.addCanvas(ko, fighterCanvas(p.color, true));
          const group = this.add.container(0, 0);
          const shadow = this.add.graphics().fillStyle(0x000000, 0.45).fillEllipse(sx, sy + 16, 46, 16).fillStyle(hex(p.color), 0.16).fillEllipse(sx, sy + 16, 40, 13);
          const ring = this.add.graphics();
          const sprite = this.add.image(sx, sy, ok).setScale(4.4).setOrigin(.5).setInteractive({ useHandCursor: true });
          sprite.flipX = sx > CX;
          sprite.on('pointerdown', () => this.selectCb && this.selectCb(p.id));
          const name = this.add.text(sx, sy - 72, p.name.toUpperCase().slice(0, 14), { fontFamily: PX, fontSize: '8px', color: '#fff' }).setOrigin(.5);
          const model = this.add.text(sx, sy - 60, (p.model || '').slice(0, 22), { fontFamily: '"IBM Plex Mono", monospace', fontSize: '9px', color: '#9aa7c7' }).setOrigin(.5);
          const bw = 72, bx = sx - bw / 2, by = sy - 48;
          const barBack = this.add.graphics().fillStyle(0x05050d, 1).fillRect(bx - 1, by - 1, bw + 2, 7).lineStyle(1, 0x000000, 1).strokeRect(bx - 1, by - 1, bw + 2, 7);
          const bar = this.add.graphics();
          const barLabel = this.add.text(sx, by + 10, 'NO THREAT READ', { fontFamily: PX, fontSize: '5px', color: '#5b6788' }).setOrigin(.5);
          const status = this.add.text(sx, sy + 32, '', { fontFamily: PX, fontSize: '7px', color: '#9aa7c7' }).setOrigin(.5);
          group.add([shadow, ring, sprite, name, model, barBack, bar, barLabel, status]);
          const breathe = reduced() ? null : this.tweens.add({ targets: sprite, scale: 4.6, duration: 650 + i * 60, yoyo: true, repeat: -1, ease: 'Sine.inOut' });
          this.fighters[p.id] = { id: p.id, home: [sx, sy], group, sprite, ring, name, model, bar, barBack, barLabel, status, bx, by, bw, breathe, alive: true, threat: null, color: hex(p.color), selected: false };
        });
      }
      players.forEach(p => {
        const f = this.fighters[p.id]; if (!f) return;
        f.name.setText(p.name.toUpperCase().slice(0, 14)); f.model.setText((p.model || '').slice(0, 22));
        if (f.alive && !p.alive) this.fall(p.id);
        else if (!f.alive && p.alive) this.rise(p.id);
        f.status.setText(p.status || '').setColor(p.statusColor || '#9aa7c7');
        f.selected = !!p.selected; this.drawRing(f);
      });
    }
    drawRing(f) {
      f.ring.clear();
      if (f.selected) f.ring.lineStyle(2, 0xffd23f, 0.9).strokeEllipse(f.home[0], f.home[1] + 16, 52, 20);
    }
    drawBar(f) {
      f.bar.clear();
      if (!f.alive) { f.barBack.setVisible(false); f.barLabel.setVisible(false); return; }
      f.barBack.setVisible(true); f.barLabel.setVisible(true);
      if (f.threat == null) { f.barLabel.setText('NO THREAT READ').setColor('#5b6788'); return; }
      const hp = Math.max(0, Math.min(1, 1 - f.threat));
      f.bar.fillStyle(threatColor(hp), 1).fillRect(f.bx, f.by, f.bw * hp, 5);
      f.barLabel.setText(`JEV THREAT ${Math.round(f.threat * 100)}%`).setColor('#9aa7c7');
    }
    setThreat(id, value) { const f = this.fighters[id]; if (!f) return; f.threat = Number.isFinite(value) ? value : null; this.drawBar(f); }
    fall(id) {
      const f = this.fighters[id]; if (!f || !f.alive) return;
      f.alive = false; f.threat = null; this.drawBar(f);
      if (f.breathe) f.breathe.pause();
      f.sprite.setTexture('ko_' + id).setScale(4).setAlpha(.85); f.name.setColor('#5b6788');
      this.crownText.setVisible(false);
    }
    rise(id) {
      const f = this.fighters[id]; if (!f) return;
      f.alive = true; f.sprite.setTexture('f_' + id).setScale(4.4).setAlpha(1); f.name.setColor('#fff'); if (f.breathe) f.breathe.resume(); this.drawBar(f);
    }
    crown(id) {
      const f = this.fighters[id];
      if (!f || !f.alive) { this.crownText.setVisible(false); return; }
      this.crownText.setVisible(true).setPosition(f.home[0], f.home[1] - 88);
    }
    pop(x, y, txt, color, big) {
      const t = this.add.text(x, y, txt, { fontFamily: PX, fontSize: (big ? 13 : 9) + 'px', color }).setOrigin(.5).setDepth(5);
      if (reduced()) { this.time.delayedCall(900, () => t.destroy()); return; }
      this.tweens.add({ targets: t, y: y - 30, alpha: 0, duration: 950, ease: 'Cubic.out', onComplete: () => t.destroy() });
    }
    strike(fromId, toId, label, heavy) {
      const from = this.fighters[fromId], to = this.fighters[toId];
      if (!from || !from.alive) return;
      if (!to) { this.pop(from.home[0], from.home[1] - 70, label || 'STRIKE', '#ff5b6e'); return; }
      const [hx, hy] = from.home, [tx, ty] = to.home;
      const ang = Math.atan2(ty - hy, tx - hx);
      const impact = () => {
        to.sprite.setTintFill(0xffffff); this.time.delayedCall(80, () => to.sprite.clearTint());
        this.cameras.main.shake(heavy ? 260 : 150, heavy ? 0.012 : 0.006);
        this.emitter.explode(heavy ? 30 : 16, tx, ty);
        const slash = this.add.text(tx, ty, '✦', { fontSize: '30px', color: '#fff' }).setOrigin(.5).setDepth(5);
        this.tweens.add({ targets: slash, scale: 2, alpha: 0, duration: 260, onComplete: () => slash.destroy() });
        this.pop(tx, ty - 70, label || 'HIT', heavy ? '#ff5b6e' : '#ffd23f', heavy);
      };
      if (reduced()) { impact(); return; }
      from.sprite.flipX = tx < hx;
      this.tweens.add({ targets: from.sprite, x: tx - Math.cos(ang) * 48, y: ty - Math.sin(ang) * 48, duration: 150, ease: 'Quad.in', yoyo: true, hold: 60,
        onYoyo: impact, onComplete: () => { from.sprite.setPosition(hx, hy); from.sprite.flipX = hx > CX; } });
    }
    guard(id) {
      const f = this.fighters[id]; if (!f || !f.alive) return;
      const ring = this.add.text(f.home[0], f.home[1], '🛡', { fontSize: '22px' }).setOrigin(.5).setDepth(5);
      this.tweens.add({ targets: ring, alpha: 0, scale: 1.6, duration: 650, onComplete: () => ring.destroy() });
      this.pop(f.home[0], f.home[1] - 70, 'GUARD', '#49c7ff');
    }
    tool(id) { const f = this.fighters[id]; if (!f || !f.alive) return; this.pop(f.home[0] + 30, f.home[1] - 60, '›_', '#9aa7c7'); }
    speak(id) { const f = this.fighters[id]; if (!f || !f.alive) return; this.pop(f.home[0] + 30, f.home[1] - 60, '…', '#9aa7c7'); }
    knockout(id, byId, confirmed) {
      const f = this.fighters[id]; if (!f) return;
      const finish = () => {
        this.fall(id);
        this.cameras.main.shake(320, 0.014);
        this.emitter.explode(40, f.home[0], f.home[1]);
        this.pop(f.home[0], f.home[1] - 78, 'ELIMINATED', '#ff5b6e', true);
        if (byId && this.fighters[byId]) this.time.delayedCall(500, () => this.pop(f.home[0], f.home[1] - 96, confirmed ? 'KERNEL CONFIRMED' : 'ATTRIBUTED', confirmed ? '#39e08a' : '#ffd23f'));
      };
      if (byId && this.fighters[byId] && this.fighters[byId].alive && !reduced()) { this.strike(byId, id, 'KO', true); this.time.delayedCall(230, finish); }
      else finish();
    }
  }

  let game = null, scene = null;
  window.ArenaStage = {
    mount(parent) {
      if (game) return;
      scene = new Scene();
      game = new Phaser.Game({ type: Phaser.AUTO, parent, width: GW, height: GH, pixelArt: true, backgroundColor: '#05050d', scene,
        scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH } });
    },
    onSelect(cb) { scene && (scene.selectCb = cb); },
    setRoster(players) { scene && scene.whenReady(() => scene.setRoster(players)); },
    setThreat(id, value) { scene && scene.whenReady(() => scene.setThreat(id, value)); },
    crown(id) { scene && scene.whenReady(() => scene.crown(id)); },
    strike(a, b, label) { scene && scene.whenReady(() => scene.strike(a, b, label)); },
    guard(id) { scene && scene.whenReady(() => scene.guard(id)); },
    tool(id) { scene && scene.whenReady(() => scene.tool(id)); },
    speak(id) { scene && scene.whenReady(() => scene.speak(id)); },
    knockout(id, by, confirmed) { scene && scene.whenReady(() => scene.knockout(id, by, confirmed)); },
  };
})();
