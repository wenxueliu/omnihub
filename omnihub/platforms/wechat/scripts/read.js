(() => {
  const textOf = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return '';
    return (el.innerText || el.textContent || '').trim();
  };
  const metaOf = (name) => {
    const el = document.querySelector('meta[property="' + name + '"]')
      || document.querySelector('meta[name="' + name + '"]');
    return el ? (el.getAttribute('content') || '').trim() : '';
  };

  const content = document.querySelector('#js_content');
  const pageText = (document.body && (document.body.innerText
    || document.body.textContent)) || '';
  // Verification markers live in the URL and DOM. Page text is
  // only a fallback: article bodies may themselves contain
  // "环境异常".
  const verify = /wappoc_appmsgcaptcha|secitptpage/i.test(location.href)
    || !!document.querySelector('#js_verify');
  if (!content) {
    if (verify) {
      return JSON.stringify({
        blocked: true,
        message: 'wechat returned an environment verification page'
      });
    }
    if (/该内容已被发布者删除|此内容因违规无法查看|内容已删除|该公众号已迁移|已停止访问该网页|无法查看/.test(pageText)) {
      return JSON.stringify({
        not_found: true,
        message: 'the WeChat article is no longer available'
      });
    }
    if (/请在微信客户端打开|环境异常/.test(pageText)) {
      return JSON.stringify({
        blocked: true,
        message: 'wechat requires its mobile client to render this page'
      });
    }
    return JSON.stringify({
      not_found: true,
      message: 'the page has no WeChat article content'
    });
  }
  const text = (content.innerText || content.textContent || '').trim();
  if (!text) {
    return JSON.stringify({
      not_found: true,
      message: 'the WeChat article has no text'
    });
  }
  const images = [];
  const seen = new Set();
  for (const img of content.querySelectorAll('img')) {
    const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
    if (src && !seen.has(src)) {
      seen.add(src);
      images.push(src);
    }
  }
  const title = textOf('#activity-name')
    || textOf('.rich_media_title')
    || metaOf('og:title')
    || (document.title || '').replace(/\s*[-–—]\s*微信公众平台\s*$/, '').trim();
  const account = textOf('#js_name')
    || textOf('.rich_media_meta_nickname')
    || metaOf('og:article:author');
  const authorName = textOf('#js_author_name');
  const published = textOf('#publish_time')
    || metaOf('og:release_date')
    || metaOf('article:published_time');
  return JSON.stringify([{
    title: title,
    url: location.href,
    author: account || authorName || null,
    published_at: published || null,
    text: text,
    account: account || null,
    author_name: authorName || null,
    images: images
  }]);
})()
