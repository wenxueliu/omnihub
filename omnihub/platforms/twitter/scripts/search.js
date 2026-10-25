(() => {
  const seen = new Set();
  const items = [];
  for (const article of document.querySelectorAll('article')) {
    const textEl = article.querySelector('[data-testid="tweetText"]');
    const nameEl = article.querySelector('[data-testid="User-Name"]');
    const timeEl = article.querySelector('time');
    const hrefs = [...article.querySelectorAll('a[href*="/status/"]')]
      .map(a => a.getAttribute('href') || '')
      .filter(h => /^\/[^/]+\/status\/\d+$/.test(h));
    const href = hrefs[0];
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const text = (textEl ? textEl.innerText : article.innerText).trim();
    items.push({
      title: text.slice(0, 120) || href,
      url: 'https://x.com' + href,
      author: nameEl ? nameEl.innerText.split('\n')[0].trim() : null,
      published_at: timeEl ? timeEl.getAttribute('datetime') : null,
      text
    });
  }
  if (!items.length) {
    if (location.href.includes('/i/flow/login') || document.querySelector('input[autocomplete="username"]')) {
      return JSON.stringify({login: true, message: 'x.com requires a logged-in browser session'});
    }
    if (/rate limit|something went wrong|try again/i.test(document.body.innerText || '')) {
      return JSON.stringify({blocked: true, message: 'x.com returned a rate-limit or error page'});
    }
  }
  return JSON.stringify(items);
})()
