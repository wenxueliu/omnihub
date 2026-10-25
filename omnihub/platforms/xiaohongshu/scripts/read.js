(() => {
  const titleEl = document.querySelector('#detail-title');
  const descEl = document.querySelector('#detail-desc, .note-content, .desc');
  const authorEl = document.querySelector('.author-wrapper .username, .username');
  const body = document.body.innerText || '';
  if (!titleEl && !descEl) {
    if (location.href.includes('/login') || document.querySelector('.login-container')) {
      return JSON.stringify({login: true, message: 'xiaohongshu requires a logged-in browser session'});
    }
    if (/安全验证|验证码|访问频繁|异常/.test(body)) {
      return JSON.stringify({blocked: true, message: 'xiaohongshu returned a verification page'});
    }
    return JSON.stringify({not_found: true, message: 'note page has no content'});
  }
  const text = (descEl ? descEl.innerText : body).trim();
  return JSON.stringify({
    url: location.href,
    title: (titleEl ? titleEl.innerText : text.slice(0, 80)).trim(),
    author: authorEl ? authorEl.innerText.trim() : null,
    text
  });
})()
