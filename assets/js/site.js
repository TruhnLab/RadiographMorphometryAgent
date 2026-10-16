// The page's small behaviours: reveal on scroll, the teaser's chapters, videos that play only while visible,
// copying the BibTeX entry, and the animated "who supplies what" diagram.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---------- reveal on scroll ----------
const revealer = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    e.target.classList.add('in');
    revealer.unobserve(e.target);
  }
}, { rootMargin: '0px 0px -8% 0px' });
$$('.reveal').forEach((el) => revealer.observe(el));

// ---------- videos play only while on screen ----------
const player = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const v = e.target;
    if (e.isIntersecting && !reduced) v.play().catch(() => {});
    else v.pause();
  }
}, { threshold: 0.35 });
$$('video').forEach((v) => player.observe(v));

// ---------- teaser chapters ----------
const teaser = $('#teaser');
const chapters = $$('#chapters button');
if (teaser && chapters.length) {
  const starts = chapters.map((b) => parseFloat(b.dataset.start));
  const tick = () => {
    const t = teaser.currentTime, end = teaser.duration || starts.at(-1) + 10;
    chapters.forEach((b, i) => {
      const a = starts[i], z = starts[i + 1] ?? end;
      const on = t >= a && t < z;
      b.classList.toggle('on', on);
      $('.fill', b).style.width = on ? `${((t - a) / (z - a)) * 100}%` : '0';
    });
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  chapters.forEach((b, i) => b.addEventListener('click', () => { teaser.currentTime = starts[i] + 0.01; teaser.play().catch(() => {}); }));
}

// ---------- copy the BibTeX entry ----------
const copy = $('#copyBib');
if (copy) {
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#bib').innerText.trim());
      copy.textContent = 'Copied';
    } catch {
      copy.textContent = 'Select and copy';
    }
    setTimeout(() => { copy.textContent = 'Copy'; }, 1800);
  });
}

// ---------- who supplies what: one case, played on a loop ----------
// Times in seconds within one cycle. The model asks the tools, the landmarks and numbers come back from the
// morphometry service (blue), the ranges from the guideline search (violet), and the model writes the text.
// Then the radiologist asks a question and finally writes in the report directly (amber).
// The landmarks and the tibial slope construction on the radiograph are the ones the app drew for this case.
const flow = $('#flow');
if (flow) {
  const CYCLE = 15.6;
  const END = 14.6;   // everything fades out from here and the case starts again
  const ON = [   // [from, to, node]: the node's outline is drawn while it works
    [0.1, 1.0, 'nImg'], [0.8, 1.9, 'nAgent'], [2.3, 3.5, 'nMorpho'], [3.8, 4.5, 'nAgent'], [5.0, 6.0, 'nGuide'],
    [6.4, 7.3, 'nAgent'], [7.5, 9.3, 'nReport'], [9.3, 10.4, 'nPerson'], [10.2, 11.2, 'nAgent'], [11.0, 12.1, 'nReport'],
    [12.2, 13.9, 'nPerson'], [12.9, 14.3, 'nReport'],
  ];
  const PACKETS = [   // [from, to, wire, colour]
    [0.2, 0.9, 'pImg', 'var(--ink)'], [1.8, 2.35, 'pMOut', 'var(--ink)'], [3.35, 3.9, 'pMIn', 'var(--num)'],
    [4.4, 4.95, 'pGOut', 'var(--ink)'], [5.95, 6.5, 'pGIn', 'var(--rng)'], [7.0, 7.6, 'pRep', 'var(--ink)'],
    [9.6, 10.3, 'pAsk', 'var(--person)'], [10.6, 11.1, 'pRep', 'var(--ink)'], [12.45, 12.95, 'pEdit', 'var(--person)'],
  ];
  const CHIPS = [[1.75, 3.4, 'cMOut'], [3.3, 5.0, 'cMIn'], [4.35, 6.0, 'cGOut'], [5.9, 7.6, 'cGIn']];
  const STATUS = [
    [0.95, 1.75, 'assesses the image: knee, lateral'], [1.75, 4.3, 'calls the morphometry service'],
    [4.3, 6.9, 'looks up the reference ranges'], [6.9, 9.4, 'writes the report'],
    [9.4, 12.2, 'answers from the same evidence'],
  ];
  const IDLE = 'runs the case, writes the text';
  const ROWS = [[7.65, 'r1'], [7.95, 'r2'], [8.25, 'r3'], [8.7, 'r4']];
  const MARKS = [3.5, .024];        // first landmark, then one every 24 ms
  const DRAW = [4.15, 4.65];        // the construction is drawn
  const TYPE = [13.0, 13.85];       // the radiologist types

  const el = (id) => document.getElementById(id);
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const ease = (u) => (u < .5 ? 2 * u * u : 1 - (-2 * u + 2) ** 2 / 2);
  const pop = (u) => (u <= 0 ? 0 : u >= 1 ? 1 : 1 + 2.2 * (u - 1) ** 3 + 1.2 * (u - 1) ** 2);   // with a little overshoot
  const win = (t, a, b, d = .25) => clamp(Math.min((t - a) / d, (b - t) / d), 0, 1);
  const dot = el('dot'), status = el('status');
  const wires = Object.fromEntries(PACKETS.map((p) => [p[2], el(p[2])]));
  const marks = el('marks'), lms = $$('.lm', marks);
  const cons = el('cons'), solid = $$('.cl:not(.dash):not(.arc)', cons), label = $('.cl-label', cons);
  const fine = $$('.cl.dash, .cl.arc', cons);
  const edit = $('#r5 text'), clip = el('editClip'), caret = el('caret');

  function draw(t) {
    for (const id of new Set(ON.map((o) => o[2])))
      el(id).classList.toggle('on', ON.some(([a, b, n]) => n === id && t >= a && t < b));
    const p = PACKETS.find(([a, b]) => t >= a && t < b);
    if (p) {
      const path = wires[p[2]], u = ease((t - p[0]) / (p[1] - p[0]));
      const pt = path.getPointAtLength(path.getTotalLength() * u);
      dot.setAttribute('cx', pt.x); dot.setAttribute('cy', pt.y);
      dot.style.fill = p[3];
      dot.style.opacity = win(t, p[0], p[1], .08);
    } else dot.style.opacity = 0;
    for (const [a, b, id] of CHIPS) el(id).style.opacity = win(t, a, b);
    const s = STATUS.find(([a, b]) => t >= a && t < b);
    const text = s ? s[2] : IDLE;
    if (status.textContent !== text) status.textContent = text;
    status.style.opacity = s ? win(t, s[0], s[1], .18) : (t < 0.95 ? win(t, -1, 0.95, .18) : win(t, 12.2, CYCLE + 1, .3));
    const out = 1 - clamp((t - END) / .45, 0, 1);

    // landmarks pop in from top to bottom, then the tibial slope construction is drawn
    lms.forEach((c, n) => {
      const u = clamp((t - MARKS[0] - n * MARKS[1]) / .22, 0, 1);
      c.setAttribute('r', (3.6 * pop(u)).toFixed(2));
      c.style.opacity = (u > 0 ? 1 : 0) * out;
    });
    marks.classList.toggle('dim', t >= DRAW[0] && t < END);
    const d = ease(clamp((t - DRAW[0]) / (DRAW[1] - DRAW[0]), 0, 1));
    for (const l of solid) {
      const len = l.getTotalLength();
      l.style.strokeDasharray = len;
      l.style.strokeDashoffset = len * (1 - d);
    }
    for (const l of fine) l.style.opacity = clamp((t - DRAW[1] + .15) / .3, 0, 1);
    label.style.opacity = clamp((t - DRAW[1]) / .3, 0, 1);
    cons.style.opacity = (t >= DRAW[0] ? 1 : 0) * out;

    for (const [a, id] of ROWS) {
      const u = clamp((t - a) / .35, 0, 1);
      el(id).style.opacity = ease(u) * out;
      el(id).setAttribute('transform', `translate(0 ${(1 - ease(u)) * 8})`);
    }
    el('hl').style.opacity = win(t, 10.9, 12.1, .3);

    // the radiologist types a sentence into the impression
    const n = edit.getNumberOfChars();
    const typed = Math.round(clamp((t - TYPE[0]) / (TYPE[1] - TYPE[0]), 0, 1) * n);
    const w = typed ? edit.getSubStringLength(0, typed) : 0;
    clip.setAttribute('width', typed ? w + 8 : 0);
    el('r5').style.opacity = out;
    caret.setAttribute('x', 726 + w + 1.5);
    const typing = t >= TYPE[0] - .1 && t < TYPE[1];
    caret.style.opacity = (typing || (t >= TYPE[1] && t < END && Math.floor((t - TYPE[1]) * 2.4) % 2 === 1)) ? 1 : 0;

    const ask = win(t, 9.3, 11.4, .25), act = win(t, 12.2, 14.2, .25);
    el('personAsk').style.opacity = ask;
    el('personEdit').style.opacity = act;
    el('personIdle').style.opacity = 1 - Math.max(ask, act);
  }

  function still() {   // the finished case, without motion
    flow.classList.add('still');
    marks.classList.add('dim');
    for (const [, id] of ROWS) el(id).style.opacity = 1;
  }

  if (reduced) still();
  else {
    draw(0);
    let raf = 0, t0 = 0, visible = false;
    const frame = (now) => {
      draw(((now - t0) / 1000) % CYCLE);
      raf = requestAnimationFrame(frame);
    };
    const start = () => { if (!raf && visible && !document.hidden) { t0 = performance.now(); raf = requestAnimationFrame(frame); } };
    const stop = () => { cancelAnimationFrame(raf); raf = 0; };
    new IntersectionObserver(([e]) => { visible = e.isIntersecting; visible ? start() : stop(); }, { threshold: 0.3 }).observe(flow);
    document.addEventListener('visibilitychange', () => (document.hidden ? stop() : start()));
  }
}
