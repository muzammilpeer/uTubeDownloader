const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const size = n => n >= 1073741824 ? `${(n/1073741824).toFixed(2)} GB` : `${(n/1048576).toFixed(1)} MB`;
const stages = {queued:'Queued',extracting:'Reading link',downloading:'Downloading',processing:'Processing',complete:'Complete',failed:'Failed',playlist:'Playlist'};
let lastRender = '';
async function refresh() {
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error('Server unavailable');
    const data = await response.json();
    $('connection').textContent = 'Live updates';
    if (data.parallel === undefined) {
      $('parallel').disabled = true;
      $('queue-capacity').textContent = 'Server restart needed to enable parallel downloads';
    } else {
      if (!$('parallel').disabled) $('parallel').value = String(data.parallel);
      $('queue-capacity').textContent = `${data.active_count} active · ${data.queued_count} queued`;
    }
    $('dependencies').textContent = data.ffmpeg ? `● FFmpeg ready${data.runtime ? '' : ' · JavaScript runtime missing'}` : '⚠ Install FFmpeg to download';
    $('folder').textContent = `Download folder: ${data.folder}`;
    const signature = JSON.stringify([data.jobs,data.files]);
    if (signature === lastRender) return;
    lastRender = signature;
    $('job-count').textContent = data.jobs.length;
    $('file-count').textContent = data.files.length;
    if (data.jobs.length) $('jobs').innerHTML = data.jobs.map(j => {
      const active = !['complete','failed'].includes(j.status);
      const details = [j.format.toUpperCase(), ['mp3','m4a'].includes(j.format) ? 'Audio' : `Up to ${j.quality}p`, j.playlist_index ? `Item ${j.playlist_index}${j.playlist_count ? ' of '+j.playlist_count : ''}` : '', j.completed_items ? `${j.completed_items} saved` : '', j.speed ? size(j.speed)+'/s' : '', j.eta ? `${Math.ceil(j.eta)}s remaining` : ''].filter(Boolean).join(' · ');
      return `<article class="job"><div class="row"><div class="title">${escapeHTML(j.title)}</div><span class="badge ${escapeHTML(j.status)}">${stages[j.status] || escapeHTML(j.status)}</span></div><div class="meta">${escapeHTML(details)}</div><div class="meta">${escapeHTML(j.message)}${j.percent != null && active ? ' · '+Math.round(j.percent)+'% of current stream' : ''}</div>${active && j.status !== 'playlist' ? `<progress aria-label="Current stream progress" max="100" ${j.percent != null ? `value="${j.percent}"` : ''}></progress>` : ''}${j.subtitle_status ? `<div class="meta">${escapeHTML(j.subtitle_status)}</div>` : ''}${!active && !j.url.includes('/playlist?') ? `<button type="button" class="retry" data-retry="${j.id}">↻ Retry & overwrite</button>` : ''}${j.warnings.length ? `<details><summary>Download notices</summary><div class="meta">${j.warnings.map(escapeHTML).join('<br>')}</div></details>` : ''}</article>`;
    }).join('');
    if (!data.files.length) $('files').innerHTML = '<div class="empty">Finished files will appear here.</div>';
    if (data.files.length) $('files').innerHTML = data.files.map(f => `<article class="file row"><div><div class="title">${escapeHTML(f.name)}</div><div class="meta">${size(f.size)} · Ready</div></div><a class="download" href="/files/${encodeURIComponent(f.job_id)}/${encodeURIComponent(f.name)}" download>↓ Download</a></article>`).join('');
  } catch(e) { $('connection').textContent = 'Connection lost · reconnecting…'; }
}
$('format').addEventListener('change', () => { $('quality').disabled = ['mp3','m4a'].includes($('format').value); });
$('download-form').addEventListener('submit', async event => {
  event.preventDefault(); $('error').hidden = true; $('submit').disabled = true;
  try {
    const response = await fetch('/api/downloads', {method:'POST',headers:{'Content-Type':'application/json','X-App-Token':document.querySelector('meta[name="app-token"]').content},body:JSON.stringify({url:$('url').value.trim(),quality:Number($('quality').value),format:$('format').value,browser:$('browser').value,playlist:$('playlist').checked,subtitles:$('subtitles').checked,subtitle_languages:$('subtitle-languages').value,thai:$('thai').checked})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Could not start download. Reload the page and try again.');
    $('url').value = ''; await refresh();
  } catch(e) { $('error').textContent = e.message; $('error').hidden = false; }
  finally { $('submit').disabled = false; }
});
(async function poll(){ await refresh(); setTimeout(poll, 1200); })();

// Optional agent access in browsers that support WebMCP.
if (document.modelContext?.registerTool) {
  const lifecycle = new AbortController();
  window.addEventListener('pagehide', () => lifecycle.abort(), {once:true});
  try {
    Promise.resolve(document.modelContext.registerTool({
      name:'read_download_status',
      description:'Read the current local download queue and finished files.',
      inputSchema:{type:'object',properties:{},additionalProperties:false},
      annotations:{readOnlyHint:true,untrustedContentHint:true},
      async execute(input) {
        if (!input || typeof input !== 'object' || Array.isArray(input) || Object.keys(input).length) throw new Error('Expected an empty object.');
        const response = await fetch('/api/status');
        if (!response.ok) throw new Error('Download service unavailable.');
        const data = await response.json();
        await refresh();
        return {jobs:data.jobs,files:data.files};
      }
    }, {signal:lifecycle.signal})).catch(() => {});
  } catch (_) { /* Standard browser controls remain available. */ }
}

$('parallel').addEventListener('change', async () => {
  $('parallel').disabled = true;
  $('settings-error').hidden = true;
  try {
    const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json','X-App-Token':document.querySelector('meta[name="app-token"]').content}, body:JSON.stringify({parallel:Number($('parallel').value)})});
    if (!response.ok) throw new Error('Could not save the limit. Reload the page and try again.');
  } catch(error) {
    $('settings-error').textContent = error.message;
    $('settings-error').hidden = false;
  } finally {
    $('parallel').disabled = false;
    await refresh();
  }
});

$('jobs').addEventListener('click', async event => {
  const button = event.target.closest('[data-retry]');
  if (!button) return;
  button.disabled = true;
  $('settings-error').hidden = true;
  try {
    const response = await fetch(`/api/downloads/${button.dataset.retry}/retry`, {method:'POST',headers:{'Content-Type':'application/json','X-App-Token':document.querySelector('meta[name="app-token"]').content},body:JSON.stringify({browser:$('browser').value,subtitles:$('subtitles').checked,subtitle_languages:$('subtitle-languages').value,thai:$('thai').checked})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Retry failed. Refresh the page and try again.');
    await refresh();
  } catch(error) { $('settings-error').hidden = false; $('settings-error').textContent = error.message; }
  finally { button.disabled = false; }
});
