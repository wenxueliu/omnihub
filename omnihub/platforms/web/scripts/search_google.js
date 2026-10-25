(() => {
  const items = [];
  const seen = new Set();
  const anchors = [...document.querySelectorAll('#rso a h3, #search a h3')];

  for (const heading of anchors) {
    const anchor = heading.closest('a');
    if (!anchor) continue;
    const href = anchor.href || '';
    if (!/^https?:\/\//i.test(href)) continue;

    let parsed;
    try {
      parsed = new URL(href);
    } catch (error) {
      continue;
    }
    const host = parsed.hostname.toLowerCase();
    // Skip Google's own navigation, consent and account links.
    if (host === 'google.com' || /(^|\.)google\.[a-z.]+$/.test(host)) continue;
    if (host === 'gstatic.com' || host.endsWith('.gstatic.com')) continue;

    const key = parsed.origin + parsed.pathname.replace(/\/+$/, '');
    if (seen.has(key)) continue;
    seen.add(key);

    // Walk up until the result card that carries the snippet. The nearest
    // ancestor with a `.VwiC3b` node is the card; the walk is capped so a
    // layout change cannot capture the entire results column.
    let card = null;
    let node = anchor;
    for (let depth = 0; depth < 10 && node && node.parentElement; depth += 1) {
      node = node.parentElement;
      if (node.querySelector && node.querySelector('.VwiC3b')) {
        card = node;
        break;
      }
    }
    const snippetEl = card ? card.querySelector('.VwiC3b') : null;
    const citeEl = card ? card.querySelector('cite') : null;
    const snippet = snippetEl
      ? (snippetEl.innerText || '').trim().replace(/\s*Read more\s*$/i, '')
      : '';
    const site = citeEl ? (citeEl.innerText || '').trim() : '';

    items.push({
      title: (heading.innerText || '').trim() || host,
      url: href.split('#')[0],
      author: null,
      text: snippet || null,
      raw: { engine: 'google', site: site || null }
    });
  }

  if (!items.length) {
    const body = document.body ? document.body.innerText || '' : '';
    if (location.pathname.startsWith('/sorry') ||
        /unusual traffic|our systems have detected|verify you are human|captcha/i.test(body)) {
      return JSON.stringify({ blocked: true, message: 'Google returned a captcha or risk-control page' });
    }
    if (location.hostname.includes('consent') ||
        /before you continue|consent to the use of cookies/i.test(body)) {
      return JSON.stringify({ blocked: true, message: 'Google requires a consent step in the browser' });
    }
  }

  return JSON.stringify(items);
})()
