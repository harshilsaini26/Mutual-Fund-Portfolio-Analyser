/*
 * The site's islands: React Bits components mounted into server-rendered pages.
 * DECISIONS V1-80.
 *
 * Every island enhances an element that already says what it means: the headline
 * is already in the heading, the figure already in the tile. With scripts off, or
 * under `prefers-reduced-motion`, nothing here runs and the page is complete.
 *
 *   [data-island="blur-text"]  the words fade in, one by one
 *   [data-island="count-up"]   the first number counts up, then the server's exact
 *                              text is put back (MODULE_6 §16.4)
 *   [data-island="spotlight"]  a soft light follows the cursor across the card
 *   [data-island="aurora"]     a moving aurora behind the hero (WebGL2 only)
 *
 * React is Preact's compatible core here (`build.mjs` aliases it): the same
 * components at a fraction of the weight.
 */
import { render } from 'preact';
import Aurora from './components/Aurora.jsx';
import BlurText from './components/BlurText.jsx';
import CountUp from './components/CountUp.jsx';
import spotlight from './components/spotlight.js';

const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function each(name, fn) {
  document.querySelectorAll(`[data-island="${name}"]`).forEach(el => {
    try {
      fn(el);
    } catch (err) {
      // An island that fails leaves the server's markup as it was.
      console.warn(`island ${name} did not mount`, err);
    }
  });
}

function blurText(el) {
  const text = el.textContent.trim();
  el.setAttribute('aria-label', text);
  // Preact's render adds to what is there; the server's words go first.
  el.textContent = '';
  render(
    <span aria-hidden="true">
      <BlurText text={text} delay={90} animateBy="words" direction="top" />
    </span>,
    el
  );
}

// "+15.4% p.a." -> "+", 15.4, "% p.a."; "Rs 1,01,793 Cr" -> "Rs ", 101793, " Cr".
const NUMBER = /^(.*?)(\d[\d,]*(?:\.\d+)?)(.*)$/s;

function countUp(el) {
  const original = el.textContent;
  const parts = NUMBER.exec(original.trim());
  if (!parts) return;
  const [, prefix, digits, suffix] = parts;
  const to = parseFloat(digits.replace(/,/g, ''));
  if (!Number.isFinite(to)) return;
  const done = () => {
    render(null, el);
    el.removeAttribute('aria-label');
    el.textContent = original;
  };
  // Mounted only when it comes into view: CountUp shows its starting value at
  // once, and a figure off-screen must never read as 0 (MODULE_6 §9.3).
  const start = () => {
    el.setAttribute('aria-label', original.trim());
    el.textContent = '';
    render(
      <span aria-hidden="true">
        {prefix}
        <CountUp to={to} separator={digits.includes(',') ? ',' : ''} duration={1.2} onEnd={done} />
        {suffix}
      </span>,
      el
    );
  };
  const seen = new IntersectionObserver(entries => {
    if (entries.some(e => e.isIntersecting)) {
      seen.disconnect();
      start();
    }
  }, { threshold: 0.6 });
  seen.observe(el);
}

// Light unless the reader chose dark, as in app.js.
function isLight() {
  return document.documentElement.getAttribute('data-theme') !== 'dark';
}

function aurora(el) {
  const probe = document.createElement('canvas');
  if (!probe.getContext('webgl2')) return;
  const draw = () =>
    render(
      <Aurora colorStops={['#7DD3FC', '#2F5FB3', '#9FB9F5']} amplitude={1.0} blend={0.55} speed={0.6}
        lightMode={isLight()} />,
      el
    );
  // Drawn only while the hero is on screen: its animation runs every frame,
  // and a reader scrolled down to the funds should not pay for it.
  let shown = false;
  new IntersectionObserver(entries => {
    const now = entries.some(e => e.isIntersecting);
    if (now === shown) return;
    shown = now;
    if (shown) draw(); else render(null, el);
  }).observe(el);
  new MutationObserver(() => shown && draw()).observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  });
}

each('spotlight', el => spotlight(el));
if (!calm) {
  each('blur-text', blurText);
  each('count-up', countUp);
  each('aurora', aurora);
}
