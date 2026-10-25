(() => {
  const items = [];
  const seen = new Set();
  for (const section of document.querySelectorAll('section.note-item')) {
    const anchors = [...section.querySelectorAll('a[href*="/search_result/"], a[href*="/explore/"]')];
    const anchor = anchors.find(a => {
      const href = a.getAttribute('href') || '';
      return /\/search_result\/[0-9a-f]+/.test(href) || /\/explore\/[0-9a-f]+/.test(href);
    });
    if (!anchor) continue;
    const href = anchor.getAttribute('href');
    const idMatch = href.match(/\/(?:search_result|explore)\/([0-9a-f]+)/);
    const id = idMatch ? idMatch[1] : href;
    if (seen.has(id)) continue;
    seen.add(id);
    const titleEl = section.querySelector('a.title, .title');
    const authorEl = section.querySelector('a.author, .author');
    const footer = section.innerText.trim();
    const title = titleEl ? titleEl.innerText.trim() : footer.split('\n')[0];
    items.push({
      title: title || id,
      url: href.startsWith('http') ? href : 'https://www.xiaohongshu.com' + href,
      author: authorEl ? authorEl.innerText.trim() : null,
      text: footer
    });
  }
  if (!items.length) {
    const body = document.body.innerText || '';
    if (location.href.includes('/login') || document.querySelector('.login-container')) {
      return JSON.stringify({login: true, message: 'xiaohongshu requires a logged-in browser session'});
    }
    if (/安全验证|验证码|访问频繁|异常/.test(body)) {
      return JSON.stringify({blocked: true, message: 'xiaohongshu returned a verification page'});
    }
  }
  return JSON.stringify(items);
})()
