'use strict';
/* Chiptune audio for the sportsbook: every sound is synthesised in the browser with
   the Web Audio API, so effects fire within a frame of the referee event that
   caused them and nothing depends on a network call. Music is a small sequencer
   whose intensity follows the match (contenders standing, final minute). */
(function () {
  let ctx = null, master = null, sfxGain = null, musicGain = null, muted = false, started = false;
  const store = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} };
  const load = (k, d) => { try { const v = JSON.parse(localStorage.getItem(k)); return v == null ? d : v; } catch (_) { return d; } };
  muted = load('gladiator-muted', false);
  let musicOn = load('gladiator-music', true);

  function ensure() {
    if (ctx) { if (ctx.state === 'suspended') ctx.resume(); return true; }
    const AC = window.AudioContext || window.webkitAudioContext; if (!AC) return false;
    ctx = new AC();
    master = ctx.createGain(); master.gain.value = muted ? 0 : 1; master.connect(ctx.destination);
    sfxGain = ctx.createGain(); sfxGain.gain.value = .55; sfxGain.connect(master);
    musicGain = ctx.createGain(); musicGain.gain.value = musicOn ? .28 : 0; musicGain.connect(master);
    started = true; music.start();
    return true;
  }
  // Browsers only let audio start after a user gesture: arm on the first one.
  ['pointerdown', 'keydown'].forEach(type => window.addEventListener(type, () => ensure(), { once: true, passive: true }));

  /* ---- synth primitives ----------------------------------------------- */
  function tone({ type = 'square', freq = 440, to = null, at = 0, dur = .12, vol = .4, slide = 'exp', dest = sfxGain }) {
    if (!ctx) return;
    const t = ctx.currentTime + at, o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type; o.frequency.setValueAtTime(freq, t);
    if (to) (slide === 'exp' ? o.frequency.exponentialRampToValueAtTime(Math.max(20, to), t + dur) : o.frequency.linearRampToValueAtTime(to, t + dur));
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(vol, t + .005); g.gain.exponentialRampToValueAtTime(.001, t + dur);
    o.connect(g); g.connect(dest); o.start(t); o.stop(t + dur + .02);
  }
  let noiseBuffer = null;
  function noise({ at = 0, dur = .15, vol = .3, cutoff = 1800, dest = sfxGain }) {
    if (!ctx) return;
    if (!noiseBuffer) { noiseBuffer = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate); const d = noiseBuffer.getChannelData(0); for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1; }
    const t = ctx.currentTime + at, s = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
    s.buffer = noiseBuffer; f.type = 'lowpass'; f.frequency.setValueAtTime(cutoff, t); f.frequency.exponentialRampToValueAtTime(200, t + dur);
    g.gain.setValueAtTime(vol, t); g.gain.exponentialRampToValueAtTime(.001, t + dur);
    s.connect(f); f.connect(g); g.connect(dest); s.start(t); s.stop(t + dur + .02);
  }
  const N = n => 440 * Math.pow(2, (n - 69) / 12); // MIDI note → Hz

  /* ---- sound effects ----------------------------------------------------- */
  const sfx = {
    strike() { noise({ dur: .12, vol: .35, cutoff: 3000 }); tone({ freq: 520, to: 90, dur: .14, vol: .35 }); },
    knockout() {
      noise({ dur: .5, vol: .5, cutoff: 1200 }); tone({ type: 'triangle', freq: 110, to: 30, dur: .55, vol: .6 });
      [72, 67, 63, 60, 55].forEach((n, i) => tone({ freq: N(n), at: .12 + i * .08, dur: .14, vol: .25 }));
    },
    guard() { tone({ freq: N(72), to: N(79), dur: .12, vol: .22 }); tone({ freq: N(79), at: .1, dur: .1, vol: .18 }); },
    tool() { tone({ freq: N(84), dur: .04, vol: .12 }); },
    speak() { tone({ type: 'triangle', freq: N(76), dur: .05, vol: .1 }); tone({ type: 'triangle', freq: N(80), at: .06, dur: .05, vol: .1 }); },
    select() { tone({ freq: N(88), dur: .05, vol: .15 }); },
    ticket() { tone({ freq: N(88), dur: .07, vol: .3 }); tone({ freq: N(95), at: .07, dur: .14, vol: .3 }); },
    payout() { [76, 80, 83, 88, 95].forEach((n, i) => tone({ freq: N(n), at: i * .07, dur: i === 4 ? .5 : .1, vol: .32 })); },
    refund() { tone({ freq: N(72), dur: .12, vol: .22 }); tone({ freq: N(67), at: .13, dur: .22, vol: .22 }); },
    error() { tone({ type: 'sawtooth', freq: 180, to: 120, dur: .22, vol: .25 }); },
    oddsUp() { tone({ freq: N(84), to: N(91), dur: .07, vol: .08 }); },
    oddsDown() { tone({ freq: N(84), to: N(77), dur: .07, vol: .08 }); },
    tick() { tone({ freq: N(96), dur: .03, vol: .14 }); },
    kickoff() { [60, 64, 67, 72].forEach((n, i) => tone({ freq: N(n), at: i * .09, dur: .16, vol: .3 })); tone({ freq: N(79), at: .4, dur: .5, vol: .32 }); },
    win() { [67, 72, 76, 79, 84, 79, 84, 91].forEach((n, i) => tone({ freq: N(n), at: i * .1, dur: i === 7 ? .8 : .14, vol: .3 })); },
    draw() { [72, 70, 69, 67].forEach((n, i) => tone({ type: 'triangle', freq: N(n), at: i * .22, dur: .3, vol: .25 })); },
  };

  /* ---- music: a lookahead-scheduled chiptune loop with intensity levels ---- */
  const music = (() => {
    const bass = [45, 45, 52, 52, 48, 48, 50, 50];                           // A minor, one bar per note pair
    const lead = [[69, 72, 76, 72], [71, 74, 79, 74], [72, 76, 79, 76], [74, 77, 81, 77]];
    const arp = [81, 84, 88, 91, 88, 84];
    let level = 0, step = 0, nextTime = 0, timer = null;
    const bpm = () => [96, 118, 132, 150][level] || 118;
    function schedule() {
      if (!ctx) return;
      const lookahead = .25, spb = 60 / bpm() / 2; // eighth notes
      while (nextTime < ctx.currentTime + lookahead) {
        const bar = Math.floor(step / 8) % 8, beat = step % 8, at = nextTime - ctx.currentTime;
        if (level >= 1) {
          if (beat % 2 === 0) tone({ type: 'triangle', freq: N(bass[bar] - 12), at, dur: spb * 1.6, vol: .5, dest: musicGain });
          if (beat % 2 === 1) noise({ at, dur: .03, vol: .12, cutoff: 6000, dest: musicGain });
          if (beat === 4) noise({ at, dur: .09, vol: .22, cutoff: 900, dest: musicGain });
        }
        if (level >= 2) tone({ freq: N(lead[bar % 4][Math.floor(beat / 2)]), at, dur: spb * .9, vol: .16, dest: musicGain });
        if (level >= 3) tone({ type: 'square', freq: N(arp[step % arp.length]), at: at + spb / 2, dur: spb * .45, vol: .1, dest: musicGain });
        if (level === 0 && beat === 0 && bar % 2 === 0) tone({ type: 'triangle', freq: N(bass[bar] + 12), at, dur: spb * 6, vol: .12, dest: musicGain });
        nextTime += spb; step++;
      }
    }
    return {
      start() { if (timer || !ctx) return; nextTime = ctx.currentTime + .05; timer = setInterval(schedule, 100); },
      setLevel(l) { l = Math.max(0, Math.min(3, l)); if (l !== level) { level = l; } },
      level: () => level,
    };
  })();

  window.ArenaAudio = {
    play(name) { if (!ctx || muted) return; const fn = sfx[name]; if (fn) try { fn(); } catch (_) {} },
    setIntensity(level) { music.setLevel(level); },
    muted: () => muted,
    musicOn: () => musicOn,
    toggleMute() { muted = !muted; store('gladiator-muted', muted); if (ensure()) master.gain.setTargetAtTime(muted ? 0 : 1, ctx.currentTime, .02); return muted; },
    toggleMusic() { musicOn = !musicOn; store('gladiator-music', musicOn); if (ensure()) musicGain.gain.setTargetAtTime(musicOn ? .28 : 0, ctx.currentTime, .05); return musicOn; },
    ready: () => started,
    ensure,
  };
})();
