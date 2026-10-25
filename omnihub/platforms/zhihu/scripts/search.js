(() => {
  const items = [];
  const seen = new Set();
  const cards = document.querySelectorAll('.SearchResult-Card, .List-item');
  for (const card of cards) {
    const titleEl = card.querySelector('.ContentItem-title');
    const anchors = [
      ...(titleEl ? titleEl.querySelectorAll('a[href]') : []),
      ...card.querySelectorAll('a[href*="/question/"], a[href*="/answer/"], a[href*="zhuanlan.zhihu.com/p/"]')
    ];
    const anchor = anchors.find(a => {
      const href = a.getAttribute('href') || '';
      return /\/question\/\d+/.test(href)
        || /(?:^|\/\/)(?:www\.)?zhuanlan\.zhihu\.com\/p\/\d+/.test(href);
    });
    if (!anchor) continue;
    let href = anchor.getAttribute('href') || '';
    if (href.startsWith('//')) href = 'https:' + href;
    else if (href.startsWith('/')) href = 'https://www.zhihu.com' + href;
    if (!/^https:\/\/(?:www|zhuanlan)\.zhihu\.com\//.test(href)) continue;
    const key = href.split(/[?#]/)[0];
    if (seen.has(key)) continue;
    seen.add(key);
    const textEl = card.querySelector('.RichContent-inner .RichText, .RichText');
    const timeEl = card.querySelector('.SearchItem-time, .ContentItem-time');
    const text = (textEl ? textEl.innerText : card.innerText).trim();
    const title = (titleEl ? titleEl.innerText : anchor.innerText || text).trim();
    // Search cards prefix the excerpt with the author, e.g. "量子位：...".
    const authorMatch = text.match(/^([^\s：:。，！？]{1,20})[：:]/);
    items.push({
      title: title || key,
      url: href,
      author: authorMatch ? authorMatch[1] : null,
      published_at: timeEl ? timeEl.innerText.trim() : null,
      text
    });
  }
  if (!items.length) {
    const body = document.body.innerText || '';
    if (location.href.includes('/signin') || document.querySelector('.SignFlowModal')) {
      return JSON.stringify({login: true, message: 'zhihu requires a logged-in browser session'});
    }
    if (/安全验证|验证码|访问过于频繁|请求异常|系统检测到/.test(body)) {
      return JSON.stringify({blocked: true, message: 'zhihu returned a verification page'});
    }
  }
  return JSON.stringify(items);
})()
