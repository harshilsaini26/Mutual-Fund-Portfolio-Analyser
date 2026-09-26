// Build the islands and copy the vendored front-end assets. DECISIONS V1-80.
//
//   cd ui && npm ci && npm run build
//
// Writes into src/m6_views/static/ and is committed, so neither CI nor a reader of
// this repository needs Node. The bundle's first line records the SHA-256 of ui/src;
// tests/unit/test_islands.py fails when the sources change and the bundle does not.
import { createHash } from 'node:crypto';
import { copyFileSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const here = dirname(fileURLToPath(import.meta.url));
const src = join(here, 'src');
const statics = join(here, '..', 'src', 'm6_views', 'static');
const vendor = join(statics, 'vendor');
const fonts = join(statics, 'fonts');
const modules = join(here, 'node_modules');

// The same walk and digest as test_islands.py: sorted relative paths, each
// followed by its bytes with line endings normalised.
function sourceDigest(dir) {
  const files = [];
  const walk = d => readdirSync(d, { withFileTypes: true }).forEach(e => {
    const p = join(d, e.name);
    if (e.isDirectory()) walk(p); else files.push(p);
  });
  walk(dir);
  const hash = createHash('sha256');
  for (const f of files.map(f => relative(dir, f).split('\\').join('/')).sort()) {
    hash.update(f + '\n');
    hash.update(readFileSync(join(dir, f), 'utf8').replace(/\r\n/g, '\n'));
  }
  return hash.digest('hex');
}

const digest = sourceDigest(src);
await build({
  entryPoints: [join(src, 'islands.jsx')],
  bundle: true,
  minify: true,
  format: 'iife',
  target: ['es2020'],
  legalComments: 'none',
  jsx: 'automatic',
  jsxImportSource: 'preact',
  alias: {
    'react': 'preact/compat',
    'react-dom': 'preact/compat',
    'react-dom/client': 'preact/compat/client',
    'react/jsx-runtime': 'preact/jsx-runtime',
    'react/jsx-dev-runtime': 'preact/jsx-runtime',
  },
  define: { 'process.env.NODE_ENV': '"production"' },
  banner: { js: `/* islands source sha256=${digest} -- React Bits components (MIT + Commons Clause), Preact, motion, ogl; built by ui/build.mjs */` },
  outfile: join(vendor, 'islands.v1.js'),
});

mkdirSync(fonts, { recursive: true });
copyFileSync(join(modules, 'lenis', 'dist', 'lenis.min.js'), join(vendor, 'lenis.v1.3.26.min.js'));
copyFileSync(join(modules, 'lenis', 'dist', 'lenis.css'), join(statics, 'lenis.css'));
copyFileSync(join(modules, '@fontsource-variable', 'rubik', 'files', 'rubik-latin-wght-normal.woff2'),
  join(fonts, 'rubik-latin-wght-normal.woff2'));

// Keep SHA256SUMS in step with what this script writes.
const sums = join(vendor, 'SHA256SUMS');
const ours = ['islands.v1.js', 'lenis.v1.3.26.min.js'];
const kept = readFileSync(sums, 'utf8').split('\n').filter(l => !ours.some(n => l.endsWith(`  ${n}`)) && l !== '');
const fresh = ours.map(n => `${createHash('sha256').update(readFileSync(join(vendor, n))).digest('hex')}  ${n}`);
writeFileSync(sums, [...kept, ...fresh].join('\n') + '\n');
console.log(`islands.v1.js built from ui/src ${digest.slice(0, 12)}`);
