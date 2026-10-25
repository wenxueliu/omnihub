(async () => {
  const idMatch = location.pathname.match(/\/jobs\/view\/(?:.*-)?(\d+)/);
  if (!idMatch) {
    return JSON.stringify({not_found: true, message: 'url has no linkedin job id'});
  }
  const response = await fetch(
    '/jobs-guest/jobs/api/jobPosting/' + idMatch[1],
    {credentials: 'same-origin'}
  );
  if (response.status === 404) {
    return JSON.stringify({not_found: true, message: 'linkedin job posting not found'});
  }
  if (!response.ok) {
    return JSON.stringify({
      blocked: true,
      message: 'linkedin job posting returned HTTP ' + response.status
    });
  }
  const html = await response.text();
  const decode = (value) => value
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(p|li|div|h\d)>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  const pick = (pattern) => {
    const match = html.match(pattern);
    return match ? decode(match[1]) : null;
  };
  const title = pick(/top-card-layout__title[^>]*>([\s\S]*?)<\/h2>/)
    || pick(/<h1[^>]*>([\s\S]*?)<\/h1>/);
  const company = pick(/topcard__org-name-link[^>]*>([\s\S]*?)<\/a>/)
    || pick(/topcard__flavor[^>]*>([\s\S]*?)<\/a>/);
  const text = pick(/show-more-less-html__markup[^>]*>([\s\S]*?)<\/div>/)
    || pick(/description__text[^>]*>([\s\S]*?)<\/div>/)
    || pick(/<body[^>]*>([\s\S]*?)<\/body>/);
  if (!text) {
    return JSON.stringify({not_found: true, message: 'job posting has no description'});
  }
  return JSON.stringify({
    url: 'https://www.linkedin.com/jobs/view/' + idMatch[1] + '/',
    title: title || 'LinkedIn job ' + idMatch[1],
    author: company,
    text
  });
})()
