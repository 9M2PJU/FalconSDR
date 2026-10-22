// Live audio: one long fetch() of raw mono PCM16 (X-Sample-Rate header) from
// /api/audio, each chunk scheduled back to back as an AudioBufferSourceNode.
// Works over plain http:// to an IP (no AudioWorklet, which needs HTTPS).
// Keeps ~0.25 s of latency and resyncs after gaps (no transmission = no data).

const LATENCY_S = 0.25;
const MAX_AHEAD_S = 1.2;

export const liveAudio = {
  ctx: null,
  gain: null,
  ctrl: null,
  playing: false,
  volume: parseFloat(localStorage.getItem('volume') ?? '0.9'),
  onchange: () => {},
  t: 0,

  async start() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.volume;
      // Through an <audio> element, not straight to the speakers: Android then treats
      // it as media playback - keeps it going with the screen off, shows controls.
      if (this.ctx.createMediaStreamDestination) {
        const dest = this.ctx.createMediaStreamDestination();
        this.gain.connect(dest);
        this.el = new Audio();
        this.el.srcObject = dest.stream;
      } else this.gain.connect(this.ctx.destination);
    }
    if (this.el) this.el.play().catch(() => {});
    await this.ctx.resume();
    if ('mediaSession' in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: 'FalconSDR', artist: 'Live' });
      navigator.mediaSession.setActionHandler('play', () => this.start());
      navigator.mediaSession.setActionHandler('pause', () => this.stop());
    }
    this.playing = true;
    this.onchange();
    if (!this.ctrl) this._stream();
  },

  stop() {
    this.playing = false;
    if (this.ctrl) { this.ctrl.abort(); this.ctrl = null; }
    if (this.el) this.el.pause();
    this.onchange();
  },

  toggle() { return this.playing ? this.stop() : this.start(); },

  setVolume(v) {
    this.volume = v;
    localStorage.setItem('volume', String(v));
    if (this.gain) this.gain.gain.value = v;
  },

  async _stream() {
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    this.t = 0;
    let rest = null;
    try {
      const r = await fetch('/api/audio', { signal: ctrl.signal, cache: 'no-store' });
      if (!r.ok) throw new Error(String(r.status));
      const rate = +(r.headers.get('X-Sample-Rate') || 12500);
      const reader = r.body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        let u = value;
        if (rest) { const m = new Uint8Array(rest.length + u.length); m.set(rest); m.set(u, rest.length); u = m; rest = null; }
        if (u.length % 2) { rest = u.slice(-1); u = u.slice(0, -1); }
        if (!u.length) continue;
        const i16 = new Int16Array(u.buffer, u.byteOffset, u.length / 2);
        const f = new Float32Array(i16.length);
        for (let i = 0; i < f.length; i++) f[i] = i16[i] / 32768;
        const buf = this.ctx.createBuffer(1, f.length, rate);
        buf.copyToChannel(f, 0);
        const src = this.ctx.createBufferSource();
        src.buffer = buf;
        src.connect(this.gain);
        const now = this.ctx.currentTime;
        if (this.t < now + 0.02 || this.t > now + MAX_AHEAD_S) this.t = now + LATENCY_S;
        src.start(this.t);
        this.t += buf.duration;
      }
    } catch { /* aborted or dropped - reconnect below */ }
    if (this.ctrl === ctrl) this.ctrl = null;
    if (this.playing && !ctrl.signal.aborted) setTimeout(() => { if (this.playing && !this.ctrl) this._stream(); }, 1000);
  },
};
