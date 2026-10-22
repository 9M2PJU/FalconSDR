// "Listen": the live scanner - what's on air now, controls, spectrum, quick
// settings, the scanned bands and the activity feed.
import { api, confirmDialog, errorToast, fmtMHz, formDialog, h, icon, mount, select, seg, slider, t, toast, toggle } from '../core.js';
import { liveAudio } from '../live-audio.js';
import { S, cfg, confirmRecording, on, patchConfig, setFastPolling } from '../state.js';

const MODULATIONS = ['AM', 'NFM', 'WFM', 'SSB'];
const OVERRIDES = ['trigger_level_dbfs', 'tuning_time_ms', 'linger_time_ms', 'gain_mode', 'gain_db', 'noise_reduction_enabled', 'noise_reduction_strength', 'squelch_enabled', 'squelch_offset_db'];
const hasOverrides = (b) => OVERRIDES.some((k) => b[k] !== null && b[k] !== undefined);
const clearOverrides = (b) => OVERRIDES.forEach((k) => { b[k] = null; });

export function stateLabel(state) {
  return {
    IDLE: t('Stopped'), CALCULATE_NEXT_FREQ: t('Scanning'), TUNING: t('Scanning'), SCANNING: t('Scanning'),
    SIGNAL_DETECTED: t('Signal'), LINGER: t('Hold'), RECONNECTING: t('Reconnecting'),
  }[state] || state;
}

function stateClass(live) {
  if (!live.running) return '';
  if (live.state === 'SIGNAL_DETECTED') return 'good live';
  if (live.state === 'LINGER') return 'warn';
  if (live.state === 'RECONNECTING') return 'bad';
  return 'accent';
}

export function listenView(root) {
  setFastPolling(true);
  const offs = [];
  const page = h('div.page');
  mount(root, page);

  // ---------------------------------------------------------- hero
  const stateChip = h('span.chip.state');
  const freqEl = h('div.freq', '—');
  const nameEl = h('div.freq-name');
  const others = h('div.others');
  const meterFill = h('div.fill');
  const meterMark = h('div.mark');
  const levelText = h('span.mono');
  const cycleText = h('span.faint.small');

  const startBtn = h('button.big.primary', { onclick: () => toggleScan() });
  const listenBtn = h('button.big', { onclick: () => liveAudio.toggle().catch(errorToast) });
  const volume = h('input', { type: 'range', min: 0, max: 1.5, step: 0.05, value: liveAudio.volume, oninput: (e) => liveAudio.setVolume(+e.target.value), title: t('Volume') });
  const skipBtn = h('button', { onclick: () => command('skip'), title: t('Leave this frequency for now') }, icon('skip'), t('Skip'));
  const blBtn = h('button', { onclick: () => command('blacklist'), title: t('Leave it and never stop on it again (adds it to the blacklist)') }, icon('ban'), t('Skip + blacklist'));
  const recBtn = h('button.recbtn', { onclick: () => toggleManualRecord(), title: t('Continuous recording of what you hear, including silence') }, icon('record'), t('Record'));
  const iqBtn = h('button.ghost.small', { onclick: () => toggleRawIq(), title: t('Raw wideband IQ (SigMF) - about 19 MB per second') }, t('Raw IQ'));
  const autoRec = toggle(t('Auto-record transmissions'), cfg().recording.enabled, async (v, e) => {
    if (v && !(await confirmRecording())) { e.target.checked = false; return; }
    patchConfig({ recording: { enabled: v } });
  });

  const hero = h('section.card.hero',
    h('div.row', stateChip, h('span.spacer'), cycleText),
    h('div', freqEl, nameEl),
    others,
    h('div.col', { style: { gap: '4px' } },
      h('div.meter', meterFill, meterMark),
      h('div.meter-legend', h('span', '-100'), levelText, h('span', '0 dBFS'))),
    h('div.controls', startBtn, h('div.row', listenBtn, h('div.volume', icon('volume'), volume)), skipBtn, blBtn, recBtn),
    h('div.row.wrap', autoRec, h('span.spacer'), iqBtn));

  // ---------------------------------------------------------- spectrum
  const canvas = h('canvas', { height: 260 });
  const axis = h('div.spectrum-axis');
  const spectrumCard = h('section.card.spectrum-card',
    h('div.card-head', h('h2', t('Spectrum')), h('span.faint.small', t('Yellow line: trigger level'))),
    h('div.spectrum-wrap', canvas), axis);
  const spectrum = spectrumPainter(canvas, axis);

  // ---------------------------------------------------------- bands
  const bandsBody = h('div.col');
  const bandsCard = h('section.card', h('div.card-head', h('h2', t('What to scan'))), bandsBody);
  const renderBands = () => mount(bandsBody, bandsSection());

  // ---------------------------------------------------------- right column
  const quick = h('section.card');
  const device = h('section.card');
  const feed = h('div.feed');
  const feedCard = h('section.card', h('div.card-head', h('h2', t('Activity'))), feed);

  mount(page, h('div.page-head', h('h1', t('Listen'))), h('div.grid.listen',
    h('div.col', { style: { gap: '16px' } }, hero, spectrumCard, bandsCard),
    h('div.col', { style: { gap: '16px' } }, quick, device, feedCard)));

  // ---------------------------------------------------------- behaviour
  async function toggleScan() {
    try {
      if (S.live && S.live.running) await api.post('/api/scanner/stop');
      else {
        await api.post('/api/scanner/start');
        if (!liveAudio.playing && localStorage.getItem('autolisten') !== '0') liveAudio.start().catch(() => {});
      }
    } catch (e) { errorToast(e); }
  }

  async function command(name, body = {}) {
    try { await api.post('/api/scanner/' + name, body); } catch (e) { errorToast(e); }
  }

  async function toggleManualRecord() {
    const active = S.live && S.live.manual_recording;
    if (!active && !(await confirmRecording())) return;
    command('manual_record', { enabled: !active });
  }

  async function toggleRawIq() {
    const active = S.live && S.live.raw_iq;
    if (!active && !(await confirmRecording())) return;
    command('raw_iq', { enabled: !active });
  }

  function renderLive() {
    const live = S.live;
    if (!live) return;
    stateChip.className = 'chip state ' + stateClass(live);
    mount(stateChip, h('span.dot'), live.stopping ? t('Stopping…') : stateLabel(live.state));
    const [primary, ...rest] = live.frequencies || [];
    const active = live.state === 'SIGNAL_DETECTED' || live.state === 'LINGER';
    if (primary) {
      mount(freqEl, fmtMHz(primary[0], 4), h('small', 'MHz'));
      nameEl.textContent = primary[1] || '';
      freqEl.style.color = active ? 'var(--signal)' : '';
    } else {
      mount(freqEl, '—');
      nameEl.textContent = live.running ? '' : t('Press Start to begin scanning');
      freqEl.style.color = '';
    }
    mount(others, active ? rest.map(([f, n]) => h('span.chip', h('b.mono', fmtMHz(f, 3)), n ? ` ${n}` : '')) : []);
    const pct = (v) => Math.max(0, Math.min(100, (v + 100)));
    meterFill.style.width = `${pct(live.level)}%`;
    const trig = live.spectrum ? live.spectrum.trigger : cfg().trigger_level_dbfs;
    meterMark.style.left = `${pct(trig)}%`;
    levelText.textContent = live.running ? `${live.level.toFixed(1)} dBFS` : '';
    cycleText.textContent = live.running && live.cycle_time ? t('Back to this window: {s} s ago', { s: live.cycle_time.toFixed(1) }) : '';
    mount(startBtn, icon(live.running ? 'stop' : 'play'), live.running ? t('Stop') : t('Start'));
    startBtn.className = 'big ' + (live.running ? 'danger' : 'primary');
    startBtn.disabled = !!live.stopping;
    [skipBtn, blBtn, recBtn, iqBtn].forEach((b) => { b.disabled = !live.running; });
    recBtn.classList.toggle('on', !!live.manual_recording);
    mount(recBtn, icon('record'), live.manual_recording ? t('Stop recording') : t('Record'));
    iqBtn.textContent = live.raw_iq ? t('Stop raw IQ') : t('Raw IQ');
    iqBtn.classList.toggle('on', !!live.raw_iq);
    spectrum.draw(live.spectrum);
    gainSlider && gainSlider.input && (gainSlider.input.min = live.gain_range[0], gainSlider.input.max = live.gain_range[1]);
    lockDevice(live.running);
    renderFeed();
  }

  function renderAudio() {
    listenBtn.classList.toggle('on', liveAudio.playing);
    mount(listenBtn, icon(liveAudio.playing ? 'headphones' : 'mute'), liveAudio.playing ? t('Listening') : t('Listen'));
  }

  let lastFeedId = -1;
  function renderFeed() {
    const last = S.log.length ? S.log[S.log.length - 1].id : 0;
    if (last === lastFeedId) return;
    lastFeedId = last;
    mount(feed, [...S.log].reverse().slice(0, 40).map((e) =>
      h('div', h('time', new Date(e.time * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })), h('span', e.text))));
  }

  // ---------------------------------------------------------- quick settings
  let gainSlider = null;
  function renderQuick() {
    const c = cfg();
    const gainMode = c.device.gain_mode;
    const range = S.live ? S.live.gain_range : [0, 50];
    gainSlider = slider({ name: gainMode === 'sw_auto' ? t('Starting gain') : t('Gain'), min: range[0], max: range[1], step: 0.1, value: c.device.gain_db, unit: 'dB', oninput: (v) => patchConfig({ device: { gain_db: v } }) });
    const squelch = slider({ name: t('Squelch below trigger'), min: -40, max: 0, step: 1, value: c.squelch_offset_db, unit: 'dB', oninput: (v) => patchConfig({ squelch_offset_db: v }) });
    squelch.input.disabled = !c.squelch_enabled;
    const nr = slider({ name: t('Noise reduction strength'), min: 0, max: 100, step: 1, value: Math.round(c.noise_reduction.strength * 100), unit: '%', oninput: (v) => patchConfig({ noise_reduction: { strength: v / 100 } }) });
    nr.input.disabled = !c.noise_reduction.enabled;
    mount(quick,
      h('div.card-head', h('h2', t('Receiver')), h('span.faint.small', t('Changes apply live'))),
      slider({ name: t('Trigger level'), min: -90, max: 0, step: 1, value: c.trigger_level_dbfs, unit: 'dBFS', oninput: (v) => patchConfig({ trigger_level_dbfs: v }) }),
      h('div.col', { style: { gap: '6px' } },
        toggle(t('Squelch'), c.squelch_enabled, (v) => { patchConfig({ squelch_enabled: v }); squelch.input.disabled = !v; }, { hint: t('Mutes the noise tail after a transmission ends') }),
        squelch),
      h('div.col', { style: { gap: '6px' } },
        h('span.small.muted', t('Gain')),
        seg([['manual', t('Manual')], ['auto', t('Auto (hardware)')], ['sw_auto', t('Auto (software)')]], gainMode, (v) => { patchConfig({ device: { gain_mode: v } }); renderQuick(); }),
        gainMode === 'auto' ? h('span.faint.small', t('The tuner sets its own gain. Can overload on very strong stations - try software auto then.')) : gainSlider),
      slider({ name: t('Tuning time'), min: 0, max: 1000, step: 10, value: c.timing.tuning_time_ms, unit: 'ms', oninput: (v) => patchConfig({ timing: { tuning_time_ms: v } }) }),
      slider({ name: t('Hold after a transmission'), min: 0, max: 30000, step: 100, value: c.timing.linger_time_ms, format: (v) => `${(v / 1000).toFixed(1)} s`, oninput: (v) => patchConfig({ timing: { linger_time_ms: v } }) }),
      h('div.col', { style: { gap: '6px' } },
        toggle(t('Noise reduction'), c.noise_reduction.enabled, (v) => { patchConfig({ noise_reduction: { enabled: v } }); nr.input.disabled = !v; }, { hint: t('Adaptive filter for weak, noisy voice') }),
        nr));
  }

  // ---------------------------------------------------------- device
  let deviceInputs = [];
  function renderDevice() {
    const c = cfg();
    const devices = S.settings.devices;
    const current = devices.findIndex((d) => d.driver === c.device.driver && d.args === c.device.device_args);
    const sel = select(devices.map((d, i) => [i, d.label]), current >= 0 ? current : 0, (v) => {
      const d = devices[+v];
      patchConfig({ device: { driver: d.driver, device_args: d.args } });
    });
    const bw = h('input', { type: 'number', min: 0.25, max: 61.44, step: 0.01, value: (c.device.sample_rate_hz / 1e6).toFixed(2), onchange: (e) => patchConfig({ device: { sample_rate_hz: +e.target.value * 1e6 } }) });
    deviceInputs = [sel, bw];
    mount(device,
      h('div.card-head', h('h2', t('SDR device'))),
      h('div.fields',
        h('label.field', t('Device'), sel),
        h('label.field', t('Capture width (MHz)'), bw)),
      h('span.faint.small.lockhint', t('Stop the scanner to change the device or capture width.')));
    lockDevice(S.live && S.live.running);
  }
  function lockDevice(running) {
    deviceInputs.forEach((el) => { el.disabled = !!running; });
    const hint = device.querySelector('.lockhint');
    if (hint) hint.classList.toggle('hidden', !running);
  }

  // ---------------------------------------------------------- bands
  function bandsSection() {
    const c = cfg();
    const presets = S.settings.presets;
    const presetSel = select([['', t('Add a band preset…')], ...presets.map((p) => [p.id, t(p.label)])], '', (id) => {
      const p = presets.find((x) => x.id === id);
      if (!p) return;
      c.bands.push({ start_hz: p.start_hz, stop_hz: p.stop_hz, step_hz: p.step_hz, label: t(p.label), enabled: true, modulation: p.modulation, weight: 0,
        trigger_level_dbfs: null, tuning_time_ms: null, linger_time_ms: null, gain_mode: null, gain_db: null, noise_reduction_enabled: null, noise_reduction_strength: null });
      patchConfig({ bands: c.bands });
      renderBands();
    });
    const enabledChannels = S.settings.channels.filter((ch) => ch.enabled).length;
    const custom = c.bands.filter(hasOverrides).length;
    return [
      custom ? h('div.row.wrap.small.muted',
        t('{n} of {total} bands use their own settings instead of the Receiver panel.', { n: custom, total: c.bands.length }),
        h('button.ghost.small', {
          onclick: async () => {
            if (!(await confirmDialog(t('Use the general settings everywhere'), t('Remove the own settings (trigger, times, gain, noise reduction) of all {n} bands? From then on every band follows the Receiver panel.', { n: custom }), t('Use general settings')))) return;
            c.bands.forEach(clearOverrides);
            patchConfig({ bands: c.bands });
            renderBands();
          },
        }, icon('refresh'), t('Use general settings in all bands'))) : null,
      c.bands.length ? c.bands.map((b, i) => bandRow(b, i)) : h('div.empty', t('Nothing to scan yet - add a band preset or a frequency below.')),
      h('div.row.wrap', presetSel,
        h('button', { onclick: addCustom }, icon('plus'), t('Custom range')),
        h('button', { onclick: addSingle }, icon('plus'), t('Single frequency'))),
      h('div.divider'),
      h('div.setting',
        h('div', toggle(t('Memory List'), c.memory_list_enabled, (v) => patchConfig({ memory_list_enabled: v }),
          { hint: t('{n} enabled channels', { n: enabledChannels }) })),
        h('a', { href: '#/channels' }, t('Edit channels →'))),
    ];
  }

  function newBand(fields) {
    return { enabled: true, modulation: 'AM', weight: 0, label: '', trigger_level_dbfs: null, tuning_time_ms: null, linger_time_ms: null,
      gain_mode: null, gain_db: null, noise_reduction_enabled: null, noise_reduction_strength: null, ...fields };
  }

  async function addCustom() {
    const v = await formDialog(t('Custom range'), [
      { key: 'label', label: t('Name'), value: '' },
      { key: 'start', label: t('Start (MHz)'), type: 'number', value: 118 },
      { key: 'stop', label: t('Stop (MHz)'), type: 'number', value: 137 },
      { key: 'step', label: t('Step (kHz)'), type: 'number', value: 25 },
      { key: 'mod', label: t('Modulation'), type: 'select', value: 'AM', options: MODULATIONS.map((m) => [m, m]) },
    ], t('Add'));
    if (!v || !(v.stop > v.start) || !(v.step > 0)) return;
    cfg().bands.push(newBand({ label: v.label || t('Custom range'), start_hz: Math.round(v.start * 1e6), stop_hz: Math.round(v.stop * 1e6), step_hz: Math.round(v.step * 1e3), modulation: v.mod }));
    patchConfig({ bands: cfg().bands });
    renderBands();
  }

  async function addSingle() {
    const v = await formDialog(t('Single frequency'), [
      { key: 'freq', label: t('Frequency (MHz)'), type: 'number', value: 121.5, hint: t('Watched alongside the bands, e.g. 121.500 MHz guard') },
      { key: 'label', label: t('Name'), value: '' },
      { key: 'mod', label: t('Modulation'), type: 'select', value: 'AM', options: MODULATIONS.map((m) => [m, m]) },
    ], t('Add'));
    if (!v || !(v.freq > 0)) return;
    const hz = Math.round(v.freq * 1e6);
    cfg().bands.push(newBand({ label: v.label || `${v.freq.toFixed(3)} MHz`, start_hz: hz, stop_hz: hz, step_hz: 25000, modulation: v.mod }));
    patchConfig({ bands: cfg().bands });
    renderBands();
  }

  const expanded = new Set();
  function bandRow(b, i) {
    const save = () => patchConfig({ bands: cfg().bands });
    const single = b.start_hz === b.stop_hz;
    const solo = S.live && S.live.solo_band === i;
    const custom = hasOverrides(b);
    const row = h('div.band', { class: (b.enabled ? '' : 'disabled') + (solo ? ' solo' : '') },
      h('div.band-main',
        h('label.switch', h('input', { type: 'checkbox', checked: b.enabled, title: t('Scan this band'), onchange: (e) => { b.enabled = e.target.checked; save(); row.classList.toggle('disabled', !b.enabled); } })),
        h('div.grow',
          h('div.title.ellipsis', b.label || t('(unnamed)'), custom ? h('span.chip.accent', { style: { marginLeft: '8px' } }, t('own settings')) : null),
          h('div.range', single ? `${fmtMHz(b.start_hz, 4)} MHz · ${b.modulation}` : `${fmtMHz(b.start_hz, 3)} – ${fmtMHz(b.stop_hz, 3)} MHz · ${(b.step_hz / 1000).toFixed(b.step_hz % 1000 ? 3 : 0)} kHz · ${b.modulation}${b.weight ? ` · W ${b.weight}` : ''}`)),
        h('button.icon.ghost', { class: solo ? 'on' : '', title: solo ? t('Back to scanning everything') : t('Scan only this band for now'), onclick: () => api.post('/api/scanner/solo', { band: solo ? null : i }).catch(errorToast) }, icon('headphones')),
        h('button.icon.ghost', { title: t('Edit'), onclick: () => { expanded.has(i) ? expanded.delete(i) : expanded.add(i); renderBands(); } }, icon(expanded.has(i) ? 'chevron' : 'edit')),
        h('button.icon.ghost', {
          title: t('Remove'), onclick: async () => {
            if (!(await confirmDialog(t('Remove band'), t('Remove “{name}” from the scan list?', { name: b.label || t('(unnamed)') }), t('Remove'), true))) return;
            cfg().bands.splice(i, 1); expanded.clear(); save(); renderBands();
          },
        }, icon('trash'))));
    if (expanded.has(i)) row.append(bandEditor(b, save));
    return row;
  }

  function bandEditor(b, save) {
    const single = b.start_hz === b.stop_hz;
    const num = (label, value, onchange, attrs = {}) => h('label.field', label, h('input', { type: 'number', value, step: 'any', ...attrs, onchange: (e) => { onchange(+e.target.value); save(); } }));
    const fields = h('div.band-edit',
      h('label.field', t('Name'), h('input', { value: b.label, onchange: (e) => { b.label = e.target.value; save(); renderBands(); } })),
      num(single ? t('Frequency (MHz)') : t('Start (MHz)'), (b.start_hz / 1e6).toFixed(4), (v) => { b.start_hz = Math.round(v * 1e6); if (single) b.stop_hz = b.start_hz; }),
      single ? null : num(t('Stop (MHz)'), (b.stop_hz / 1e6).toFixed(4), (v) => { b.stop_hz = Math.round(v * 1e6); }),
      single ? null : num(t('Step (kHz)'), b.step_hz / 1000, (v) => { b.step_hz = Math.round(v * 1000); }),
      h('label.field', t('Modulation'), select(MODULATIONS.map((m) => [m, m]), b.modulation, (v) => { b.modulation = v; save(); })),
      num(t('Priority weight'), b.weight, (v) => { b.weight = v; }, { min: 0, max: 20, step: 0.5, title: t('0 = normal. Higher = visited more often than other weighted bands (1 vs 2 = twice as often).') }));
    // Per-band overrides: unticked = follow the global setting. Values that
    // only make sense together (on/off + strength, mode + gain) share one tile.
    const c = cfg();
    const defaults = {
      trigger_level_dbfs: c.trigger_level_dbfs, tuning_time_ms: c.timing.tuning_time_ms, linger_time_ms: c.timing.linger_time_ms,
      gain_mode: c.device.gain_mode, gain_db: c.device.gain_db,
      noise_reduction_enabled: c.noise_reduction.enabled, noise_reduction_strength: c.noise_reduction.strength,
      squelch_enabled: c.squelch_enabled, squelch_offset_db: c.squelch_offset_db,
    };
    const ov = (keys, label, makeInput, globalValue) => {
      const own = () => keys.some((k) => b[k] !== null && b[k] !== undefined);
      const holder = h('div.col', { style: { gap: '8px' } });
      const draw = () => mount(holder, own() ? makeInput(draw) : h('span.faint.small', t('Global: {v}', { v: globalValue })));
      draw();
      return h('div.override',
        h('label.switch', h('input', {
          type: 'checkbox', checked: own(), title: t('Own value for this band'),
          onchange: (e) => { keys.forEach((k) => { b[k] = e.target.checked ? defaults[k] : null; }); save(); draw(); },
        }), h('span.label', label)),
        holder);
    };
    const rangeInput = (key, min, max, step, fmt, scale = 1) => slider({ name: '', min, max, step, value: (b[key] ?? defaults[key]) * scale, format: fmt, oninput: (v) => { b[key] = v / scale; save(); } });
    const gainModes = [['manual', t('Manual')], ['auto', t('Auto (hardware)')], ['sw_auto', t('Auto (software)')]];
    const overrides = h('div.overrides',
      ov(['trigger_level_dbfs'], t('Trigger level'), () => rangeInput('trigger_level_dbfs', -90, 0, 1, (v) => `${v} dBFS`), `${c.trigger_level_dbfs} dBFS`),
      ov(['squelch_enabled', 'squelch_offset_db'], t('Squelch'), (redraw) => {
        const on = b.squelch_enabled ?? defaults.squelch_enabled;
        const offset = rangeInput('squelch_offset_db', -40, 0, 1, (v) => `${v} dB`);
        offset.input.disabled = !on;
        return [toggle(t('On'), on, (v) => { b.squelch_enabled = v; save(); redraw(); }), offset];
      }, c.squelch_enabled ? `${t('On')}, ${c.squelch_offset_db} dB` : t('Off')),
      ov(['tuning_time_ms'], t('Tuning time'), () => rangeInput('tuning_time_ms', 0, 1000, 10, (v) => `${v} ms`), `${c.timing.tuning_time_ms} ms`),
      ov(['linger_time_ms'], t('Hold after a transmission'), () => rangeInput('linger_time_ms', 0, 30000, 100, (v) => `${(v / 1000).toFixed(1)} s`), `${(c.timing.linger_time_ms / 1000).toFixed(1)} s`),
      ov(['gain_mode', 'gain_db'], t('Gain'), (redraw) => [
        select(gainModes, b.gain_mode ?? defaults.gain_mode, (v) => { b.gain_mode = v; save(); redraw(); }),
        (b.gain_mode ?? defaults.gain_mode) === 'auto' ? null : rangeInput('gain_db', 0, 50, 0.1, (v) => `${(+v).toFixed(1)} dB`),
      ], `${gainModes.find(([k]) => k === c.device.gain_mode)?.[1] ?? c.device.gain_mode}, ${c.device.gain_db} dB`),
      ov(['noise_reduction_enabled', 'noise_reduction_strength'], t('Noise reduction'), (redraw) => {
        const on = b.noise_reduction_enabled ?? defaults.noise_reduction_enabled;
        const strength = rangeInput('noise_reduction_strength', 0, 100, 1, (v) => `${v}%`, 100);
        strength.input.disabled = !on;
        return [toggle(t('On'), on, (v) => { b.noise_reduction_enabled = v; save(); redraw(); }), strength];
      }, c.noise_reduction.enabled ? `${t('On')}, ${Math.round(c.noise_reduction.strength * 100)}%` : t('Off')));
    return h('div.col', fields,
      h('div.row.wrap', h('h3', t('Own settings for this band')), h('span.spacer'),
        hasOverrides(b) ? h('button.ghost.small', { onclick: () => { clearOverrides(b); save(); renderBands(); } }, icon('refresh'), t('Restore general settings')) : null),
      overrides);
  }

  // ---------------------------------------------------------- wiring
  renderQuick();
  renderDevice();
  renderBands();
  renderAudio();
  renderLive();
  offs.push(on('live', renderLive), on('audio', renderAudio), on('settings', () => { renderQuick(); renderDevice(); renderBands(); }));
  let lastSolo = S.live && S.live.solo_band;
  offs.push(on('live', (live) => { if (live.solo_band !== lastSolo) { lastSolo = live.solo_band; renderBands(); } }));
  return { destroy() { offs.forEach((f) => f()); setFastPolling(false); spectrum.destroy(); } };
}

// Spectrum line + scrolling waterfall of the current capture window.
function spectrumPainter(canvas, axis) {
  const SPEC_H = 140;
  const ctx = canvas.getContext('2d');
  const water = document.createElement('canvas');
  const wctx = water.getContext('2d');
  let lo = -100, hi = -20, window_ = '';
  let last = null;

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(200, canvas.clientWidth) * dpr;
    if (canvas.width !== w) {
      canvas.width = w; canvas.height = 260 * dpr;
      water.width = w; water.height = (260 - SPEC_H) * dpr;
    }
    return dpr;
  }
  const ro = new ResizeObserver(() => { resize(); draw(last, true); });
  ro.observe(canvas);

  function color(v) {  // 0..1 -> dark blue .. cyan .. yellow .. red
    const stops = [[5, 9, 30], [20, 60, 140], [30, 170, 220], [250, 220, 70], [250, 70, 50]];
    const x = Math.max(0, Math.min(0.999, v)) * (stops.length - 1);
    const i = Math.floor(x), f = x - i;
    return stops[i].map((c, k) => Math.round(c + (stops[i + 1][k] - c) * f));
  }

  function draw(sp, redrawOnly = false) {
    const dpr = resize();
    const W = canvas.width, SH = SPEC_H * dpr;
    ctx.fillStyle = '#050913';
    ctx.fillRect(0, 0, W, canvas.height);
    if (!sp || !sp.db.length) {
      ctx.fillStyle = '#5f6b85';
      ctx.font = `${12 * dpr}px system-ui`;
      ctx.fillText(t('No data - start the scanner'), 14 * dpr, 24 * dpr);
      axis.replaceChildren();
      return;
    }
    last = sp;
    const db = sp.db;
    const key = `${sp.f0}-${sp.f1}`;
    if (key !== window_) { window_ = key; wctx.clearRect(0, 0, water.width, water.height); }
    const mn = Math.min(...db), mx = Math.max(...db, sp.trigger);
    lo += ((mn - 6) - lo) * 0.2;
    hi += ((mx + 8) - hi) * 0.2;
    const y = (v) => SH - ((v - lo) / (hi - lo)) * SH;
    // grid
    ctx.strokeStyle = 'rgba(120,140,180,.12)';
    ctx.lineWidth = 1;
    for (let g = Math.ceil(lo / 10) * 10; g < hi; g += 10) { ctx.beginPath(); ctx.moveTo(0, y(g)); ctx.lineTo(W, y(g)); ctx.stroke(); }
    // spectrum fill + line
    const accent = sp.custom ? '#c084fc' : '#38bdf8';
    const grad = ctx.createLinearGradient(0, 0, 0, SH);
    grad.addColorStop(0, sp.custom ? 'rgba(192,132,252,.45)' : 'rgba(56,189,248,.45)');
    grad.addColorStop(1, 'rgba(56,189,248,0)');
    ctx.beginPath();
    ctx.moveTo(0, SH);
    db.forEach((v, i) => ctx.lineTo((i / (db.length - 1)) * W, y(v)));
    ctx.lineTo(W, SH);
    ctx.fillStyle = grad;
    ctx.fill();
    ctx.beginPath();
    db.forEach((v, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, (i / (db.length - 1)) * W, y(v)));
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.5 * dpr;
    ctx.stroke();
    // trigger
    ctx.setLineDash([6 * dpr, 4 * dpr]);
    ctx.strokeStyle = '#fbbf24';
    ctx.lineWidth = 1.2 * dpr;
    ctx.beginPath(); ctx.moveTo(0, y(sp.trigger)); ctx.lineTo(W, y(sp.trigger)); ctx.stroke();
    ctx.setLineDash([]);
    // waterfall: shift down one row, paint the new one on top
    if (!redrawOnly) {
      const row = wctx.createImageData(water.width, 1);
      for (let x = 0; x < water.width; x++) {
        const v = db[Math.floor((x / water.width) * db.length)];
        const [r, g, b] = color((v - lo) / (hi - lo));
        row.data.set([r, g, b, 255], x * 4);
      }
      wctx.drawImage(water, 0, 1 * dpr);
      for (let k = 0; k < Math.max(1, Math.round(dpr)); k++) wctx.putImageData(row, 0, k);
    }
    ctx.drawImage(water, 0, SH);
    const mid = (sp.f0 + sp.f1) / 2;
    axis.replaceChildren(...[sp.f0, mid, sp.f1].map((f) => h('span', `${fmtMHz(f, 3)} MHz`)));
  }

  return { draw: (sp) => draw(sp), destroy: () => ro.disconnect() };
}
