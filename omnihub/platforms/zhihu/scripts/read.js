(() => {
  const body = document.body.innerText || '';
  const login = location.href.includes('/signin')
    || Boolean(document.querySelector('.SignFlowModal'))
    || /登录后查看全部|登录后继续|请先登录|扫码登录/.test(body);
  const blocked = /安全验证|验证码|访问过于频繁|请求异常|系统检测到您的网络环境/.test(body);

  const answerIdMatch = location.pathname.match(/\/answer\/(\d+)/);
  let title = '';
  let author = null;
  let text = '';
  const postTitle = document.querySelector('.Post-Title');
  const postBody = document.querySelector('.Post-RichText');
  if (postTitle && postBody) {
    // Zhuanlan article (zhuanlan.zhihu.com/p/<id>).
    title = postTitle.innerText.trim();
    text = postBody.innerText.trim();
    const authorEl = document.querySelector('.Post-Author .AuthorInfo-name, .AuthorInfo-name');
    author = authorEl ? authorEl.innerText.trim() : null;
  } else {
    // Question or answer page.
    const questionTitle = document.querySelector('.QuestionHeader-title');
    title = questionTitle ? questionTitle.innerText.trim() : '';
    let answers = [...document.querySelectorAll('.AnswerItem')];
    if (answerIdMatch) {
      answers = answers.filter(answer => answer.getAttribute('name') === answerIdMatch[1]);
    } else {
      answers = answers.slice(0, 3);
    }
    const chunks = [];
    for (const answer of answers) {
      const content = answer.querySelector('.RichContent-inner .RichText, .RichText');
      if (!content) continue;
      const authorEl = answer.querySelector('.AuthorInfo-name');
      const timeEl = answer.querySelector('.ContentItem-time');
      const header = [
        authorEl ? authorEl.innerText.trim() : '',
        timeEl ? timeEl.innerText.trim() : ''
      ].filter(Boolean).join(' · ');
      chunks.push((header ? header + '\n' : '') + content.innerText.trim());
    }
    text = chunks.join('\n\n---\n\n');
    const firstAuthor = answers[0] ? answers[0].querySelector('.AuthorInfo-name') : null;
    author = firstAuthor ? firstAuthor.innerText.trim() : null;
  }

  if (!text) {
    if (login) {
      return JSON.stringify({login: true, message: 'zhihu requires a logged-in browser session'});
    }
    if (blocked) {
      return JSON.stringify({blocked: true, message: 'zhihu returned a verification page'});
    }
    return JSON.stringify({not_found: true, message: 'zhihu page has no readable content'});
  }

  const maxChars = 60000;
  const truncated = text.length > maxChars;
  if (truncated) text = text.slice(0, maxChars);
  return JSON.stringify({
    url: location.href,
    title: title || (document.title || '').replace(/\s*-\s*知乎$/, ''),
    author,
    text,
    truncated,
    truncated_reason: truncated ? 'max_chars' : null
  });
})()
