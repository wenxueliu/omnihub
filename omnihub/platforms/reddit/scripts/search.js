(async () => {
  const params = new URLSearchParams(location.search);
  const query = params.get('q') || '';
  const limit = Number(params.get('limit')) || 10;
  if (!query) return JSON.stringify([]);
  const endpoint = '/search.json?' + new URLSearchParams({
    q: query,
    limit: String(limit),
    type: 'link',
    sort: 'relevance',
    raw_json: '1'
  }).toString();
  const response = await fetch(endpoint, {
    credentials: 'same-origin',
    headers: {Accept: 'application/json'}
  });
  if (response.status === 401 || response.status === 403) {
    return JSON.stringify({login: true, message: 'reddit search requires a logged-in browser session'});
  }
  if (response.status === 429) {
    return JSON.stringify({blocked: true, message: 'reddit rate-limited the search request'});
  }
  if (!response.ok) return JSON.stringify([]);
  const payload = await response.json();
  const children = (payload && payload.data && payload.data.children) || [];
  const items = [];
  for (const child of children) {
    const post = child && child.data;
    if (!post || !post.title) continue;
    const permalink = post.permalink || '';
    items.push({
      title: post.title,
      url: permalink ? 'https://www.reddit.com' + permalink : post.url,
      author: post.author ? 'u/' + post.author : null,
      published_at: post.created_utc
        ? new Date(post.created_utc * 1000).toISOString()
        : null,
      text: (post.selftext || '').trim() || null,
      id: post.id,
      subreddit: post.subreddit_name_prefixed,
      score: post.score,
      comments: post.num_comments
    });
  }
  return JSON.stringify(items);
})()
