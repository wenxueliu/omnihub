(() => {
  const titleEl = document.querySelector('h1');
  const contentEl = document.querySelector(
    '.article__bd__detail, .status-content, .timeline__item__content'
  );
  const userEl = document.querySelector('.user-name, a.user-name');
  const body = document.body.innerText || '';
  if (!contentEl) {
    if (location.href.includes('/login') || /登录后/.test(body)) {
      return JSON.stringify({login: true, message: 'xueqiu requires a logged-in browser session'});
    }
    if (/访问过于频繁|安全验证|验证码/.test(body)) {
      return JSON.stringify({blocked: true, message: 'xueqiu returned a verification page'});
    }
    return JSON.stringify({not_found: true, message: 'status page has no content'});
  }
  const text = contentEl.innerText.trim();
  return JSON.stringify({
    url: location.href,
    title: (titleEl ? titleEl.innerText : text).trim().slice(0, 120),
    author: userEl ? userEl.innerText.trim() : null,
    text
  });
})()
