/*
 * SpotlightCard, from React Bits (https://reactbits.dev, src/content/Components/SpotlightCard),
 * MIT + Commons Clause: used here as part of this website, not redistributed as a
 * component.
 *
 * The component is a wrapper whose only behaviour is this mouse handler; its look is
 * CSS (`.card-spotlight` in app.css). The cards here are server-rendered -- a fund's
 * name, its figures, its links -- so rather than rebuild them inside a component, the
 * same handler is attached to each one. What a reader sees is identical.
 */
export default function spotlight(el, spotlightColor = 'rgba(19, 69, 133, 0.12)') {
  el.classList.add('card-spotlight');
  el.addEventListener('mousemove', e => {
    const rect = el.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    el.style.setProperty('--mouse-x', `${x}px`);
    el.style.setProperty('--mouse-y', `${y}px`);
    el.style.setProperty('--spotlight-color', spotlightColor);
  });
}
