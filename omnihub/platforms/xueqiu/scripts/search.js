(() => {
  const items = [];
  const seen = new Set();
  for (const node of document.querySelectorAll('.timeline__item')) {
    const link = node.querySelector('a[data-id], a[href*="/status/"]');
    const href = link ? link.getAttribute('href') : null;
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const titleEl = node.querySelector('.timeline__item__title');
    const contentEl = node.querySelector('.timeline__item__content');
    const userEl = node.querySelector('a.user-name');
    const dateEl = node.querySelector('.date-and-source');
    const content = (contentEl ? contentEl.innerText : node.innerText).trim();
    items.push({
      title: (titleEl ? titleEl.innerText : content).trim().slice(0, 120),
      url: href.startsWith('http') ? href : 'https://xueqiu.com' + href,
      author: userEl ? userEl.innerText.trim() : null,
      published_at: dateEl ? dateEl.innerText.trim() : null,
      text: content
    });
  }
  if (!items.length) {
    const body = document.body.innerText || '';
    if (location.href.includes('/login') || /登录后/.test(body)) {
      return JSON.stringify({login: true, message: 'xueqiu requires a logged-in browser session'});
    }
    if (/访问过于频繁|安全验证|验证码/.test(body)) {
      return JSON.stringify({blocked: true, message: 'xueqiu returned a verification page'});
    }
  }
  return JSON.stringify(items);
})()
