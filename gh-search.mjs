// GitHub repo search helper: prints a compact table, one line per repo.
// Usage: node gh-search.mjs "<query>" [perPage]
const q = process.argv[2];
const perPage = process.argv[3] || '25';

const url = `https://api.github.com/search/repositories?q=${encodeURIComponent(q)}&sort=stars&order=desc&per_page=${perPage}`;

const res = await fetch(url, {
  headers: { 'User-Agent': 'dsh-agent-survey', 'Accept': 'application/vnd.github+json' },
});
if (!res.ok) {
  console.log(`HTTP ${res.status}`);
  console.log((await res.text()).slice(0, 500));
  process.exit(1);
}
const j = await res.json();
console.log(`# query: ${q}   total=${j.total_count}   shown=${j.items.length}`);
for (const r of j.items) {
  const d = (r.description || '').replace(/\s+/g, ' ').slice(0, 110);
  console.log(
    `${String(r.stargazers_count).padStart(7)} | ${r.full_name.padEnd(46)} | ${(r.language || '-').padEnd(11)} | ${String(r.license?.spdx_id || '-').padEnd(14)} | ${r.pushed_at.slice(0, 10)} | ${d}`
  );
}
