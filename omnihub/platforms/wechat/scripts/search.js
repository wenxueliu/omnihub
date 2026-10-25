(() => {
  const items = [];
  const seen = new Set();
  for (const entry of document.querySelectorAll('ul.news-list li')) {
    const anchor = entry.querySelector('h3 a');
    if (!anchor || !anchor.href) continue;
    const url = anchor.href;
    if (seen.has(url)) continue;
    seen.add(url);
    const account = entry.querySelector('.s-p a, .account');
    const summary = entry.querySelector('.txt-info');
    const stamp = entry.querySelector('.s-p .s2');
    items.push({
      title: (anchor.innerText || '').trim(),
      url: url,
      author: account ? (account.innerText || '').trim() : null,
      published_at: stamp ? (stamp.innerText || '').trim() : null,
      text: summary ? (summary.innerText || '').trim() : ''
    });
  }
  if (!items.length) {
    const body = document.body.innerText || '';
    if (/antispider|请输入验证码|安全验证|访问过于频繁/.test(location.href + ' ' + body)) {
      return JSON.stringify({
        blocked: true,
        message: 'sogou weixin search returned a verification page'
      });
    }
  }
  return JSON.stringify(items);
})()
