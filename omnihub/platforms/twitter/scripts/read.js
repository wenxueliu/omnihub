(() => {
  const article = document.querySelector('article');
  if (!article) {
    if (location.href.includes('/i/flow/login') || document.querySelector('input[autocomplete="username"]')) {
      return JSON.stringify({login: true, message: 'x.com requires a logged-in browser session'});
    }
    if (/rate limit|something went wrong|try again/i.test(document.body.innerText || '')) {
      return JSON.stringify({blocked: true, message: 'x.com returned a rate-limit or error page'});
    }
    return JSON.stringify({not_found: true, message: 'tweet page has no article'});
  }
  const textEl = article.querySelector('[data-testid="tweetText"]');
  const nameEl = article.querySelector('[data-testid="User-Name"]');
  const text = article.innerText.trim();
  return JSON.stringify({
    url: location.href,
    title: (textEl ? textEl.innerText : text).slice(0, 120),
    author: nameEl ? nameEl.innerText.split('\n')[0].trim() : null,
    text
  });
})()
