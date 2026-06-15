// 260614-cannobio-logistics-check-v1.0.js
// Ablage: .github/scripts/check_logistics.js
//
// Prüft ob sich FART 316, VCO Linie 3/1 oder SAF Alibus PDFs geändert haben.
// Muster nach supplementcheck-Projektdokumentation:
//   – Playwright Chromium: besucht jede Site für Bot-Check und Cookies
//   – context.request.get(): PDF-Download mit dem Cookie-Store des Browser-Kontexts
//   – Magic-Byte-Prüfung: %PDF (kein HEAD-Request, kein Content-Type-Vertrauen)
//   – SHA-256-Vergleich mit .github/cannobio-logistics-versions.json
//
// Exit 0: alles erledigt (changed=true/false im GITHUB_OUTPUT)
// Exit 1: fataler Fehler
//
// Abhängigkeiten: npm install playwright (in .github/scripts/)

'use strict';
const { chromium } = require('playwright');
const crypto       = require('crypto');
const fs           = require('fs');
const path         = require('path');

const CACHE_DIR   = path.resolve('cannobio/logistics/data/_cache');
const VER_FILE    = path.resolve('.github/cannobio-logistics-versions.json');
const GH_OUTPUT   = process.env.GITHUB_OUTPUT || '/dev/null';
const TODAY       = new Date().toISOString().slice(0, 10);

const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
         + '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

// ─── PDF-Quellen ─────────────────────────────────────────────────────────────
const SOURCES = [
  {
    key:     'fart316',
    site:    'https://www.fartiamo.ch',
    referer: 'https://www.fartiamo.ch/orari/linea-316/',
    url:     'https://fartiamo.ch/wp-content/uploads/2025/12/62316.pdf',
    // Aktuelle URL dynamisch aus der Seite entdecken (Pfad ändert sich jährlich)
    discover: /https:\/\/fartiamo\.ch\/wp-content\/uploads\/\d+\/\d+\/62316\.pdf/
  },
  {
    key:     'vco3',
    site:    'https://www.vcotrasporti.it',
    referer: 'https://www.vcotrasporti.it/it/orari.php',
    url:     'https://www.vcotrasporti.it/userdata/banner/VERBANIA-BRISSAGO.pdf'
  },
  {
    key:     'vco1',
    site:    'https://www.vcotrasporti.it',
    referer: 'https://www.vcotrasporti.it/it/orari.php',
    url:     'https://www.vcotrasporti.it/userdata/banner/VERBANIA%20-%20OMEGNA.pdf'
  },
  {
    key:     'saf',
    site:    'https://www.safduemila.com',
    referer: 'https://www.safduemila.com/linee/alibus-malpensa-lago-maggiore/',
    url:     'https://www.safduemila.com/wp-content/uploads/2019/01/alibus-2026-web.pdf',
    // Dateiname enthält das Jahres-Suffix (alibus-2027-web.pdf etc.)
    discover: /https:\/\/www\.safduemila\.com\/wp-content\/uploads\/[^"'<>\s]+alibus-\d{4}-web\.pdf/
  }
];

// ─── Hilfsfunktionen ─────────────────────────────────────────────────────────
const sha256 = buf => crypto.createHash('sha256').update(buf).digest('hex');

function loadVersions() {
  try { return JSON.parse(fs.readFileSync(VER_FILE, 'utf8')); }
  catch { return {}; }
}

function saveVersions(v) {
  fs.writeFileSync(VER_FILE, JSON.stringify(v, null, 2));
}

function appendOutput(key, val) {
  fs.appendFileSync(GH_OUTPUT, `${key}=${val}\n`);
}

// ─── Hauptprogramm ───────────────────────────────────────────────────────────
async function main() {
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  const versions   = loadVersions();
  let   anyChanged = false;

  const browser = await chromium.launch({ headless: true });

  try {
    for (const src of SOURCES) {
      console.log(`\n── ${src.key} ────────────────────────────────────────`);

      // Frischer Browser-Kontext pro Quelle (saubere Cookie-Isolation)
      const context = await browser.newContext({ userAgent: UA });

      try {
        const page = await context.newPage();

        // 1. Site besuchen: 'load' abwarten (robuster als domcontentloaded bei Weiterleitungen)
        console.log(`→ Besuche: ${src.referer}`);
        await page.goto(src.referer, { waitUntil: 'load', timeout: 30_000 });

        // 2. Aktuelle PDF-URL aus Seiteninhalt entdecken (bei jährlich wechselnden Dateinamen)
        //    page.content() in try/catch: schlägt fehl wenn Seite noch navigiert
        let pdfUrl = src.url;
        if (src.discover) {
          try {
            await page.waitForLoadState('networkidle', { timeout: 8_000 }).catch(() => {});
            const html  = await page.content();
            const match = html.match(src.discover);
            if (match && match[0] !== src.url) {
              console.log(`   Neue URL entdeckt: ${match[0]}`);
              pdfUrl = match[0];
            }
          } catch (e) {
            console.log(`   URL-Discovery übersprungen (${e.message.slice(0, 60)}) – nutze bekannte URL`);
          }
        }

        // 3. PDF laden via context.request.get() (teilt Cookie-Store des Browser-Kontexts)
        console.log(`→ Lade: ${pdfUrl}`);
        const resp = await context.request.get(pdfUrl, {
          headers: {
            'Referer': src.referer,
            'Accept':  'application/pdf,*/*;q=0.9'
          },
          timeout: 90_000
        });

        if (resp.status() === 404) {
          console.log(`⚠ 404 – PDF nicht gefunden: ${pdfUrl}`);
          await context.close();
          continue;
        }
        if (resp.status() !== 200) {
          console.log(`⚠ HTTP ${resp.status()} – übersprungen`);
          await context.close();
          continue;
        }

        const body = await resp.body();

        // 4. Magic-Byte-Prüfung (%PDF) – verlässlicher als Content-Type
        if (body.slice(0, 4).toString('ascii') !== '%PDF') {
          console.log(`⚠ Kein PDF (Magic Bytes fehlen). Erste 120 Bytes: ${body.slice(0, 120)}`);
          await context.close();
          continue;
        }

        const sizeKb = Math.round(body.length / 1024);
        const hash   = sha256(body);
        const oldRec = versions[src.key] || {};
        const oldHash = oldRec.sha256 || '';

        console.log(`✓ PDF ${sizeKb} KB · SHA-256: ${hash.slice(0, 16)}…`);
        console.log(`  Alt: ${oldHash.slice(0, 16) || '<keiner>'}`);

        if (hash !== oldHash) {
          const dest = path.join(CACHE_DIR, `${src.key}.pdf`);
          fs.writeFileSync(dest, body);
          versions[src.key] = { sha256: hash, url: pdfUrl, last_updated: TODAY };
          console.log(`→ GEÄNDERT – gespeichert: ${dest}`);
          anyChanged = true;
        } else {
          console.log(`→ unverändert`);
        }

      } finally {
        await context.close();
      }
    }

  } finally {
    await browser.close();
  }

  // Versions-Datei immer schreiben (last_checked aktualisieren)
  for (const src of SOURCES) {
    if (versions[src.key]) versions[src.key].last_checked = TODAY;
  }
  saveVersions(versions);

  appendOutput('changed', anyChanged ? 'true' : 'false');
  console.log(`\n══ Ergebnis: changed=${anyChanged} ══`);
}

main().catch(err => {
  console.error('[FATAL]', err);
  process.exit(1);
});
