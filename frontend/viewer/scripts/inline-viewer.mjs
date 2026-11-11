#!/usr/bin/env node
/**
 * Post-build step for the offline viewer.
 *
 * Vite emits an HTML shell, a JS entry and a stylesheet. To make the viewer
 * genuinely single-file (and usable from `file://`, where module-script fetches
 * are blocked), this script inlines the JS and CSS and embeds the default
 * fixture bundle as application/json. The relative bundle file is still emitted
 * alongside; a served copy fetches it and falls back to the embedded copy.
 *
 * The CSP in the source HTML allows inline script/style only, no external
 * script, font, image or connect origin beyond `self`. Inlining therefore does
 * not widen what the page can reach.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(here, '../../..');
const viewerDir = path.join(projectRoot, 'docs/viewer');
const htmlPath = path.join(viewerDir, 'index.html');
const bundlePath = path.join(viewerDir, 'decision-runs.v1.bundle.json');
const assetsDir = path.join(viewerDir, 'assets');

if (!fs.existsSync(htmlPath)) {
  throw new Error(`Built viewer HTML is missing at ${htmlPath}. Run vite build --config vite.viewer.config.ts first.`);
}
if (!fs.existsSync(bundlePath)) {
  throw new Error(`Fixture bundle is missing at ${bundlePath}. Run viewer:fixtures before building.`);
}

let html = fs.readFileSync(htmlPath, 'utf8');
const bundleText = fs.readFileSync(bundlePath, 'utf8');
const bundle = JSON.parse(bundleText);
if (bundle?.bundle_kind !== 'alphaagent.offline-viewer') {
  throw new Error('The default bundle is not an offline-viewer bundle; refusing to embed it.');
}

const scriptMatch = html.match(/<script\b[^>]*\bsrc="\.\/assets\/([^"]+\.js)"[^>]*><\/script>/i);
const styleMatch = html.match(/<link\b[^>]*\bhref="\.\/assets\/([^"]+\.css)"[^>]*>/i);
if (!scriptMatch || !styleMatch) {
  throw new Error('Could not find the Vite JS/CSS asset tags to inline. The build output shape changed.');
}

const jsPath = path.join(assetsDir, scriptMatch[1]);
const cssPath = path.join(assetsDir, styleMatch[1]);
if (!jsPath.startsWith(assetsDir) || !cssPath.startsWith(assetsDir)) {
  throw new Error('Refusing to inline an asset outside the viewer assets directory.');
}
const js = fs.readFileSync(jsPath, 'utf8');
const css = fs.readFileSync(cssPath, 'utf8');

// `</script` inside an inline script would close the element. Escaping the
// slash is valid JavaScript and leaves the program unchanged.
const inlineJs = js.replace(/<\/script/gi, '<\\/script');
const inlineCss = css.replace(/<\/style/gi, '<\\/style');

// JSON escapes are valid inside a JSON script block and cannot terminate it.
const embeddedBundle = bundleText
  .replace(/</g, '\\u003c')
  .replace(/>/g, '\\u003e')
  .replace(/&/g, '\\u0026');

const embeddedBlock = `<script type="application/json" id="viewer-default-bundle">${embeddedBundle}</script>`;
const moduleBlock = `<script type="module">${inlineJs}</script>`;
const styleBlock = `<style>${inlineCss}</style>`;

html = html.replace(scriptMatch[0], () => `${embeddedBlock}\n    ${moduleBlock}`);
html = html.replace(styleMatch[0], () => styleBlock);

// The inlined page must not reference an external/asset URL for its program.
// The inline JavaScript itself can contain the text `<script>`, so this checks
// for the original asset references rather than every occurrence of the word.
if (/<script\b[^>]*\bsrc=["']\.\/assets\//i.test(html)) {
  throw new Error('Viewer HTML still references an external script after inlining.');
}
if (/<link\b[^>]*\bhref=["']\.\/assets\//i.test(html)) {
  throw new Error('Viewer HTML still references an external stylesheet after inlining.');
}

fs.writeFileSync(htmlPath, html, 'utf8');

// The JS/CSS chunks are now duplicated inside index.html. Remove them only
// after the inlined page has been written successfully; the bundle JSON stays.
if (assetsDir.startsWith(viewerDir + path.sep) && fs.existsSync(assetsDir)) {
  fs.rmSync(assetsDir, { recursive: true, force: true });
}

const bytes = fs.statSync(htmlPath).size;
console.log(`INLINED ${path.relative(projectRoot, htmlPath)} (${(bytes / 1024).toFixed(1)} KiB)`);
console.log(`  embedded default bundle: ${(bundleText.length / 1024).toFixed(1)} KiB`);
console.log(`  relative bundle kept at: ${path.relative(projectRoot, bundlePath)}`);
