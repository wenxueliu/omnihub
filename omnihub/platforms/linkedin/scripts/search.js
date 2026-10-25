(async () => {
  const params = new URLSearchParams(location.search);
  const keywords = params.get('keywords') || '';
  const locationFilter = params.get('location') || '';
  const endpoint = '/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords='
    + encodeURIComponent(keywords)
    + '&location=' + encodeURIComponent(locationFilter)
    + '&start=0';
  const response = await fetch(endpoint, {credentials: 'same-origin'});
  if (!response.ok) {
    return JSON.stringify({
      blocked: true,
      message: 'linkedin guest job search returned HTTP ' + response.status
    });
  }
  const html = await response.text();
  const decode = (value) => value
    .replace(/<[^>]+>/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  const pick = (chunk, pattern) => {
    const match = chunk.match(pattern);
    return match ? decode(match[1]) : null;
  };
  const items = [];
  const seen = new Set();
  for (const chunk of html.split(/<li\b/).slice(1)) {
    const hrefMatch = chunk.match(/href="([^"]*\/jobs\/view\/[^"]*)"/);
    if (!hrefMatch) continue;
    const url = hrefMatch[1]
      .replace(/&amp;/g, '&')
      .split('?')[0];
    const idMatch = url.match(/\/jobs\/view\/(?:.*-)?(\d+)/);
    if (!idMatch || seen.has(idMatch[1])) continue;
    seen.add(idMatch[1]);
    const timeMatch = chunk.match(/<time[^>]*datetime="([^"]+)"/);
    items.push({
      title: pick(chunk, /base-search-card__title"[^>]*>([\s\S]*?)<\//) || 'Job ' + idMatch[1],
      url,
      author: pick(chunk, /base-search-card__subtitle"[^>]*>([\s\S]*?)<\//),
      published_at: timeMatch ? timeMatch[1] : null,
      text: pick(chunk, /job-search-card__location"[^>]*>([\s\S]*?)<\//)
    });
  }
  if (!items.length) {
    if (location.href.includes('/authwall') || location.href.includes('/login')) {
      return JSON.stringify({login: true, message: 'linkedin requires a logged-in browser session'});
    }
    return JSON.stringify({not_found: true, message: 'linkedin guest search returned no jobs'});
  }
  return JSON.stringify(items);
})()
