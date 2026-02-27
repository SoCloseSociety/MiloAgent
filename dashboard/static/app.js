'use strict';
// ══════════════════════════════════════════════════════════════
// MiloAgent Mission Control v4.0 — Dashboard Engine
// ══════════════════════════════════════════════════════════════

let TOKEN = localStorage.getItem('milo_token') || '';
let paused = false;
let currentTab = 'command';
let charts = {};
let ws = null;
let refreshTimer = null;
let loginAttempts = 0;
let loginLockUntil = 0;
let _editingProject = '';
let _logCount = 0;
let _cookieAccounts = {};
let _refreshing = false;
let _statHistory = {};     // {key: [last N values]} for sparklines
let _prevStats = {};       // previous stat values for trend arrows
let _particleAnim = null;
let _d3Loaded = false;
let _networkSim = null;

// ══════════════════════════════════════════════════════════════
// TOAST SYSTEM (stacked)
// ══════════════════════════════════════════════════════════════
function toast(msg, type) {
  const container = document.getElementById('toastContainer');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'toast ' + (type||'info');
  el.textContent = msg;
  container.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  const dur = 3500;
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.remove(), 300);
  }, dur);
  // Limit stack
  while (container.children.length > 5) container.removeChild(container.firstChild);
}

// ══════════════════════════════════════════════════════════════
// COUNT-UP ANIMATION
// ══════════════════════════════════════════════════════════════
function countUp(el, target, duration) {
  if (!el) return;
  const start = parseInt(el.textContent) || 0;
  if (start === target) return;
  const diff = target - start;
  const startTime = performance.now();
  const d = duration || 600;
  function tick(now) {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / d, 1);
    const eased = 1 - Math.pow(1 - progress, 3); // easeOutCubic
    el.textContent = Math.round(start + diff * eased);
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ══════════════════════════════════════════════════════════════
// SPARKLINE SVG
// ══════════════════════════════════════════════════════════════
function sparkline(data, w, h, color) {
  w = w || 80; h = h || 22; color = color || '#00f0ff';
  if (!data || data.length < 2) return '';
  const max = Math.max(...data, 1);
  const pts = data.map((v, i) =>
    `${(i / (data.length - 1)) * w},${h - (v / max) * (h - 2) - 1}`
  ).join(' ');
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="display:block"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" opacity=".8"/></svg>`;
}

function pushHistory(key, value) {
  if (!_statHistory[key]) _statHistory[key] = [];
  _statHistory[key].push(value);
  if (_statHistory[key].length > 20) _statHistory[key].shift();
}

function trendArrow(key, current) {
  const prev = _prevStats[key];
  _prevStats[key] = current;
  if (prev === undefined || prev === current) return '<span class="trend flat">━</span>';
  const diff = current - prev;
  const pct = prev > 0 ? Math.round(Math.abs(diff) / prev * 100) : 0;
  if (diff > 0) return `<span class="trend up">▲${pct > 0 ? pct + '%' : ''}</span>`;
  return `<span class="trend down">▼${pct > 0 ? pct + '%' : ''}</span>`;
}

// ══════════════════════════════════════════════════════════════
// SVG GAUGE
// ══════════════════════════════════════════════════════════════
function drawGauge(score, grade) {
  const r = 60, stroke = 8;
  const circumference = 2 * Math.PI * r;
  const pct = Math.min(score, 100) / 100;
  const dashOffset = circumference * (1 - pct * 0.75); // 270deg arc
  const gradeColor = {'A+':'#22ff88','A':'#22ff88','B':'#00f0ff','C':'#eab308','D':'#ff6b35','F':'#ef4444'}[grade] || '#00f0ff';
  return `<div class="perf-gauge-wrap"><svg width="150" height="150" viewBox="0 0 150 150" class="gauge-svg">
    <circle cx="75" cy="75" r="${r}" fill="none" stroke="rgba(26,39,68,.5)" stroke-width="${stroke}" stroke-dasharray="${circumference}" stroke-dashoffset="${circumference * 0.25}" transform="rotate(135 75 75)"/>
    <circle cx="75" cy="75" r="${r}" fill="none" stroke="${gradeColor}" stroke-width="${stroke}" stroke-linecap="round" stroke-dasharray="${circumference}" stroke-dashoffset="${dashOffset}" transform="rotate(135 75 75)" style="transition:stroke-dashoffset 1s cubic-bezier(.4,0,.2,1);filter:drop-shadow(0 0 6px ${gradeColor})"/>
    <text x="75" y="68" text-anchor="middle" fill="${gradeColor}" font-family="Orbitron,sans-serif" font-size="32" font-weight="900">${esc(grade)}</text>
    <text x="75" y="90" text-anchor="middle" fill="#7a8ba8" font-family="JetBrains Mono,monospace" font-size="12">${score}/100</text>
  </svg></div>`;
}

// ══════════════════════════════════════════════════════════════
// LOGIN
// ══════════════════════════════════════════════════════════════
function toggleVis() {
  const inp = document.getElementById('loginPass');
  const btn = inp.nextElementSibling;
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = 'hide'; }
  else { inp.type = 'password'; btn.textContent = 'show'; }
}

async function doLogin() {
  const now = Date.now();
  const errEl = document.getElementById('loginError');
  const btn = document.getElementById('btnLogin');
  if (now < loginLockUntil) { errEl.textContent = `Too many attempts. Wait ${Math.ceil((loginLockUntil-now)/1000)}s`; return; }
  const user = document.getElementById('loginUser').value.trim();
  const pass = document.getElementById('loginPass').value.trim();
  if (!user || !pass) { errEl.textContent = 'Username and password required'; return; }
  btn.disabled = true; btn.textContent = 'INITIALIZING...'; errEl.textContent = '';
  try {
    const r = await fetch('/api/auth/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:user,password:pass}) });
    const data = await r.json();
    if (r.ok && data.ok) { TOKEN=data.token; localStorage.setItem('milo_token',TOKEN); loginAttempts=0; showDashboard(); }
    else { loginAttempts++; if(loginAttempts>=5){loginLockUntil=Date.now()+30000;errEl.textContent='Too many attempts. Locked 30s'}else errEl.textContent=data.detail||'Invalid credentials'; }
  } catch(e) { errEl.textContent = 'Connection failed'; }
  btn.disabled = false; btn.textContent = 'LAUNCH CONTROL';
}

function showDashboard() { document.getElementById('loginPage').style.display='none'; document.getElementById('dashboardPage').style.display='block'; startDashboard(); }
function showLogin() { document.getElementById('loginPage').style.display='flex'; document.getElementById('dashboardPage').style.display='none'; document.getElementById('loginError').textContent=''; }
function logout() { TOKEN=''; localStorage.removeItem('milo_token'); if(ws){ws.close();ws=null} if(refreshTimer){clearInterval(refreshTimer);refreshTimer=null} charts={}; showLogin(); }

document.addEventListener('DOMContentLoaded', () => {
  const passEl = document.getElementById('loginPass');
  const userEl = document.getElementById('loginUser');
  if (passEl) passEl.addEventListener('keydown', e => { if(e.key==='Enter') doLogin(); });
  if (userEl) userEl.addEventListener('keydown', e => { if(e.key==='Enter') document.getElementById('loginPass').focus(); });
});

// ══════════════════════════════════════════════════════════════
// TAB NAVIGATION + HAMBURGER
// ══════════════════════════════════════════════════════════════
function switchTab(name) {
  currentTab = name;
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.tab===name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id==='tab-'+name));
  // Close mobile menu
  const navTabs = document.querySelector('.nav-tabs');
  if (navTabs) navTabs.classList.remove('open');
  const hamburger = document.querySelector('.hamburger');
  if (hamburger) hamburger.classList.remove('open');
  refresh();
}

function toggleMobileMenu() {
  const navTabs = document.querySelector('.nav-tabs');
  const hamburger = document.querySelector('.hamburger');
  if (navTabs) navTabs.classList.toggle('open');
  if (hamburger) hamburger.classList.toggle('open');
}

// ══════════════════════════════════════════════════════════════
// API HELPERS
// ══════════════════════════════════════════════════════════════
async function api(path) {
  const r = await fetch(path, {headers:{'Authorization':'Bearer '+TOKEN}});
  if (r.status===401) { logout(); throw new Error('Unauthorized'); }
  return r.json();
}
async function apiPost(path, body) {
  const opts = {method:'POST', headers:{'Authorization':'Bearer '+TOKEN}};
  if (body) { opts.headers['Content-Type']='application/json'; opts.body=JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (r.status===401) { logout(); throw new Error('Unauthorized'); }
  return r.json();
}
async function apiPut(path, body) {
  const r = await fetch(path, {method:'PUT', headers:{'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'}, body:JSON.stringify(body)});
  if (r.status===401) { logout(); throw new Error('Unauthorized'); }
  return r.json();
}
async function apiDelete(path) {
  const r = await fetch(path, {method:'DELETE', headers:{'Authorization':'Bearer '+TOKEN}});
  if (r.status===401) { logout(); throw new Error('Unauthorized'); }
  return r.json();
}

// ══════════════════════════════════════════════════════════════
// HELPERS
// ══════════════════════════════════════════════════════════════
function esc(s) { if(!s) return ''; const d=document.createElement('div'); d.textContent=String(s); return d.innerHTML; }
function fmtUp(s) { if(s<3600)return Math.floor(s/60)+'m'; if(s<86400)return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m'; return Math.floor(s/86400)+'d '+Math.floor((s%86400)/3600)+'h'; }
function fmtCD(s) { if(s<0)return'paused'; if(s<60)return s+'s'; if(s<3600)return Math.floor(s/60)+'m'; return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m'; }
function resBar(label, pct, warn, crit) {
  const color = pct>=crit?'var(--red)':pct>=warn?'var(--yellow)':'var(--neon-green)';
  return `<div class="res-bar"><span class="bar-label">${label}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.min(100,pct)}%;background:${color}"></div></div><span class="bar-pct" style="color:${color}">${pct}%</span></div>`;
}
function hpBar(status) {
  const map={healthy:5,cooldown:3,warned:2,banned:0};
  const filled=map[status]??0;
  let h=`<div class="hp-bar ${status}">`;
  for(let i=0;i<5;i++) h+=`<span class="${i<filled?'filled':''}"></span>`;
  return h+'</div>';
}
function splitCSV(s) { return s ? s.split(',').map(x=>x.trim()).filter(Boolean) : []; }

// ══════════════════════════════════════════════════════════════
// RENDER: STATUS
// ══════════════════════════════════════════════════════════════
function renderStatus(d) {
  paused = d.paused;
  const orb = document.getElementById('statusOrb');
  const txt = document.getElementById('statusText');
  if (orb) orb.className = 'status-orb '+(d.emergency_stopped?'stopped':paused?'paused':'live');
  if (txt) {
    txt.textContent = d.emergency_stopped?'STOPPED':paused?'Paused':'Online';
    txt.style.color = d.emergency_stopped?'var(--red)':paused?'var(--yellow)':'var(--neon-green)';
  }
  const mcMode = document.getElementById('mcMode');
  const uptime = document.getElementById('uptime');
  const mcVer = document.getElementById('mcVersion');
  if (mcMode) mcMode.textContent = d.mode||'auto';
  if (uptime) uptime.textContent = fmtUp(d.uptime_seconds);
  if (mcVer) mcVer.textContent = 'v'+(d.version||'4.0');
  const btnPause = document.getElementById('btnPause');
  const btnResume = document.getElementById('btnResume');
  if (btnPause) btnPause.disabled = paused;
  if (btnResume) btnResume.disabled = !paused;
  const eb = document.getElementById('emergencyBanner');
  if (eb) eb.classList.toggle('show', !!d.emergency_stopped);
  const btnEmergency = document.getElementById('btnEmergency');
  if (btnEmergency) btnEmergency.disabled = !!d.emergency_stopped;
}

// ══════════════════════════════════════════════════════════════
// RENDER: STATS + METRICS RIBBON
// ══════════════════════════════════════════════════════════════
function renderStats(d) {
  if (d.error) return;
  const total = d.total_actions||0;
  const byPlat = d.by_platform||{};
  const opps = d.opportunities||{};
  const byType = d.by_type||{};

  // Ribbon
  const mrA = document.getElementById('mrActions'); if (mrA) countUp(mrA, total, 500);
  const mrR = document.getElementById('mrReddit'); if (mrR) countUp(mrR, byPlat.reddit||0, 500);
  const mrT = document.getElementById('mrTelegram'); if (mrT) countUp(mrT, byPlat.telegram||0, 500);
  const mrO = document.getElementById('mrOpps'); if (mrO) countUp(mrO, opps.pending||0, 500);

  // Track history for sparklines
  pushHistory('total', total);
  pushHistory('reddit', byPlat.reddit||0);
  pushHistory('telegram', byPlat.telegram||0);
  pushHistory('opps', opps.pending||0);

  // Stat cards with sparklines + trends
  const row = document.getElementById('statsRow');
  if (!row) return;

  function sc(cls, val, label, key, color) {
    const trend = trendArrow(key, val);
    const spark = sparkline(_statHistory[key], 70, 18, color || '#00f0ff');
    return `<div class="stat-card ${cls}"><div class="sv">${val}</div><div class="sl">${label}</div>${trend}<div class="spark">${spark}</div></div>`;
  }

  let h = sc('green', total, 'Total 24h', 'total', '#22ff88');
  if (byPlat.reddit!==undefined) h += sc('orange', byPlat.reddit, 'Reddit', 'reddit', '#ff6b35');
  if (byPlat.telegram!==undefined) h += sc('cyan', byPlat.telegram, 'Telegram', 'telegram', '#00f0ff');
  if (opps.pending) h += sc('yellow', opps.pending, 'Opportunities', 'opps', '#eab308');
  if (opps.acted) h += sc('blue', opps.acted||0, 'Acted', 'acted', '#3b82f6');
  if (byType.comment) { pushHistory('comments', byType.comment); h += sc('purple', byType.comment, 'Comments', 'comments', '#a855f7'); }
  if (byType.post||byType.seed_post) { const pv=(byType.post||0)+(byType.seed_post||0); pushHistory('posts', pv); h += sc('orange', pv, 'Posts', 'posts', '#ff6b35'); }

  // Efficiency score
  if (opps.pending || opps.acted) {
    const totalOpps = (opps.pending||0) + (opps.acted||0) + (opps.expired||0) + (opps.rejected||0);
    const efficiency = totalOpps > 0 ? Math.round((opps.acted||0) / totalOpps * 100) : 0;
    pushHistory('efficiency', efficiency);
    h += sc('cyan', efficiency + '%', 'Efficiency', 'efficiency', '#00f0ff');
  }
  row.innerHTML = h;

  // Chart (doughnut) with gradient
  const labels = Object.keys(byType);
  const values = Object.values(byType);
  const chartColors = ['#3b82f6','#22ff88','#a855f7','#eab308','#ef4444','#00f0ff','#ff6b35','#ec4899'];
  if (charts.actions) { charts.actions.data.labels=labels; charts.actions.data.datasets[0].data=values; charts.actions.update(); }
  else if (labels.length) {
    charts.actions = new Chart(document.getElementById('chartActions').getContext('2d'), {
      type:'doughnut', data:{labels, datasets:[{data:values,backgroundColor:chartColors,borderWidth:0,hoverOffset:8,borderRadius:2}]},
      options:{responsive:true,maintainAspectRatio:false,cutout:'68%',
        animation:{duration:800,easing:'easeOutQuart'},
        plugins:{legend:{position:'right',labels:{color:'#7a8ba8',font:{size:11,family:'Exo 2'},padding:8,usePointStyle:true,pointStyleWidth:8}}}}
    });
  }
}

// ══════════════════════════════════════════════════════════════
// RENDER: TIMELINE
// ══════════════════════════════════════════════════════════════
function renderTimeline(d) {
  if (d.error || !d.hourly) return;
  const last72 = d.hourly.slice(-72);
  const labels = last72.map(h => { const p=h.hour.split('T'); return p[1]||p[0]; });
  const reddit = last72.map(h => h.reddit||0);
  const telegram = last72.map(h => h.telegram||0);

  if (charts.timeline) {
    charts.timeline.data.labels=labels;
    charts.timeline.data.datasets[0].data=reddit;
    charts.timeline.data.datasets[1].data=telegram;
    charts.timeline.update();
  } else {
    const ctx = document.getElementById('chartTimeline');
    if (!ctx) return;
    const context = ctx.getContext('2d');
    // Gradient for Reddit
    const gradR = context.createLinearGradient(0,0,0,220);
    gradR.addColorStop(0,'rgba(255,107,53,.2)');
    gradR.addColorStop(1,'rgba(255,107,53,0)');
    // Gradient for Telegram
    const gradT = context.createLinearGradient(0,0,0,220);
    gradT.addColorStop(0,'rgba(0,240,255,.15)');
    gradT.addColorStop(1,'rgba(0,240,255,0)');

    charts.timeline = new Chart(context, {
      type:'line', data:{labels, datasets:[
        {label:'Reddit',data:reddit,borderColor:'#ff6b35',backgroundColor:gradR,fill:true,tension:.4,pointRadius:0,borderWidth:2},
        {label:'Telegram',data:telegram,borderColor:'#00f0ff',backgroundColor:gradT,fill:true,tension:.4,pointRadius:0,borderWidth:2}
      ]},
      options:{responsive:true,maintainAspectRatio:false,
        animation:{duration:800,easing:'easeOutQuart'},
        interaction:{mode:'index',intersect:false},
        plugins:{legend:{labels:{color:'#7a8ba8',font:{size:11,family:'Exo 2'},usePointStyle:true,pointStyleWidth:8}},
          tooltip:{backgroundColor:'rgba(10,15,30,.9)',borderColor:'rgba(0,240,255,.2)',borderWidth:1,titleFont:{family:'Exo 2'},bodyFont:{family:'JetBrains Mono',size:11},padding:10,cornerRadius:8}
        },
        scales:{x:{ticks:{color:'#4a5b73',font:{size:10,family:'JetBrains Mono'},maxTicksLimit:12},grid:{color:'rgba(26,39,68,.4)'}},y:{ticks:{color:'#4a5b73',font:{family:'JetBrains Mono'}},grid:{color:'rgba(26,39,68,.4)'},beginAtZero:true}}}
    });
  }
}

// ══════════════════════════════════════════════════════════════
// RENDER: MINIMAPS
// ══════════════════════════════════════════════════════════════
function renderMinimaps(d) {
  const rm = document.getElementById('redditMap');
  const reddit = d.reddit||[];
  const rc = document.getElementById('redditCount');
  if (rc) rc.textContent = reddit.length;
  if (!reddit.length) { if(rm) rm.innerHTML='<p class="no-data">No subreddit data yet</p>'; }
  else if (rm) {
    const maxAct = Math.max(...reddit.map(s=>s.count_24h||1));
    rm.innerHTML = reddit.map(s => {
      const stageColor = {new:'var(--text3)',warming:'var(--yellow)',established:'var(--neon-green)',trusted:'var(--neon-cyan)'}[s.stage]||'var(--text3)';
      const pct = Math.round((s.count_24h||0)/maxAct*100);
      return `<div class="minimap-row"><span class="minimap-name" style="color:var(--neon-orange)">r/${esc(s.subreddit)}</span><div class="minimap-bar"><div class="fill" style="width:${pct}%;background:var(--neon-orange)"></div></div><span class="minimap-count">${s.count_24h}</span><span class="minimap-stage" style="color:${stageColor}">${esc(s.stage)}</span></div>`;
    }).join('');
  }

  const tm = document.getElementById('telegramPanel');
  const tgData = d.telegram||{};
  const tgGroups = tgData.groups||tgData.by_type||[];
  const tgCount = Array.isArray(tgGroups) ? tgGroups.length : 0;
  const tc = document.getElementById('telegramCount');
  if (tc) tc.textContent = tgCount;
  if (!tgCount) { if(tm) tm.innerHTML='<p class="no-data">No Telegram activity yet</p>'; }
  else if (tm) {
    tm.innerHTML = tgGroups.map(g => {
      const name = g.name||g.type||g.group||'Unknown';
      const count = g.count||g.messages||0;
      return `<div class="minimap-row"><span class="minimap-name" style="color:var(--neon-cyan)">${esc(name)}</span><div class="minimap-bar"><div class="fill" style="width:${Math.min(100,count*10)}%;background:var(--neon-cyan)"></div></div><span class="minimap-count">${count}</span></div>`;
    }).join('');
  }
}

// ══════════════════════════════════════════════════════════════
// RENDER: REDDIT ACCOUNT PERFORMANCE
// ══════════════════════════════════════════════════════════════
function renderRedditAcctPerf(d) {
  const el = document.getElementById('redditAcctPerf');
  const countEl = document.getElementById('redditAcctCount');
  if (!d||!d.length) { if(el) el.innerHTML='<p class="no-data">No Reddit accounts</p>'; if(countEl) countEl.textContent='0'; return; }
  if (countEl) countEl.textContent = d.length;

  if (el) el.innerHTML = '<div class="acct-grid">' + d.map(a => {
    const sessionClass = a.has_reddit_session ? '' : ' no-session';
    const statusBadge = a.status==='healthy' ? '<span class="badge healthy">Healthy</span>' :
      a.status==='cooldown' ? `<span class="badge cooldown">CD ${a.cooldown_remaining}s</span>` :
      `<span class="badge error">${esc(a.status)}</span>`;
    const sessionWarning = !a.has_reddit_session ? '<span style="color:var(--red);font-size:10px">NO SESSION</span>' : '';
    const successColor = a.success_rate>=0.9?'var(--neon-green)':a.success_rate>=0.7?'var(--yellow)':'var(--red)';
    let subsHtml = '';
    if (a.subreddits_active&&a.subreddits_active.length) {
      subsHtml = '<div class="acct-subs">' + a.subreddits_active.slice(0,8).map(s => `<span>r/${esc(s)}</span>`).join('') +
        (a.subreddits_active.length>8?`<span>+${a.subreddits_active.length-8}</span>`:'') + '</div>';
    }
    return `<div class="acct-card${sessionClass}"><div class="acct-header"><div><span class="acct-name">@${esc(a.username)}</span> <span class="acct-persona">${esc(a.persona)}</span>${sessionWarning}</div>${statusBadge}</div><div class="acct-stats"><div class="acct-stat"><div class="asv" style="color:var(--neon-green)">${a.total_24h}</div><div class="asl">24h</div></div><div class="acct-stat"><div class="asv" style="color:var(--blue)">${a.comments}</div><div class="asl">Comments</div></div><div class="acct-stat"><div class="asv" style="color:var(--neon-purple)">${a.posts}</div><div class="asl">Posts</div></div><div class="acct-stat"><div class="asv" style="color:${successColor}">${Math.round(a.success_rate*100)}%</div><div class="asl">Success</div></div></div><div class="acct-meta"><span>Upvotes: ${a.upvotes||0}</span><span>Subs: ${a.subscribes||0}</span><span>Active: ${a.subreddits_count} subs</span><span>4h: ${a.total_4h} acts</span>${a.cookie_age_hours!==null?`<span>Cookie: ${a.cookie_age_hours}h</span>`:''}</div>${subsHtml}</div>`;
  }).join('') + '</div>';
}

// ══════════════════════════════════════════════════════════════
// RENDER: SCHEDULE
// ══════════════════════════════════════════════════════════════
function renderSchedule(d, elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!d||!d.length) { el.innerHTML='<p class="no-data">No scheduled jobs</p>'; return; }
  el.innerHTML = d.map(j => `<div class="sched-row"><span class="sched-name">${esc(j.name)}</span><div><span class="countdown">${fmtCD(j.seconds_until)}</span><span class="interval">${esc(j.interval||'')}</span></div></div>`).join('');
}

// ══════════════════════════════════════════════════════════════
// RENDER: ACTIONS FEED
// ══════════════════════════════════════════════════════════════
function renderActions(d) {
  const el = document.getElementById('actionsFeed');
  if (!el) return;
  if (d.error||!d||!d.length) { el.innerHTML='<p class="no-data">No recent actions</p>'; return; }
  el.innerHTML = d.slice(0,50).map(a => {
    const t=a.created_at||a.timestamp||''; const time=t?t.split(' ').pop().substring(0,5):'';
    const typeColor = {comment:'var(--neon-green)',post:'var(--blue)',seed_post:'var(--neon-purple)',like:'var(--yellow)',reply:'var(--neon-cyan)'}[a.action_type]||'var(--text2)';
    return `<div class="feed-item"><span class="time">${esc(time)}</span><span class="type" style="color:${typeColor}">${esc(a.action_type||'?')}</span><span class="msg">${esc(a.platform||'')} ${esc(a.subreddit||a.target||'')} ${a.project?'<span style="color:var(--text3)">('+esc(a.project)+')</span>':''}</span></div>`;
  }).join('');
}

// ══════════════════════════════════════════════════════════════
// RENDER: CONVERSATIONS
// ══════════════════════════════════════════════════════════════
function renderConversations(d) {
  const dms = d.dms||[], alerts = d.alerts||[];
  const dmEl = document.getElementById('dmList');
  if (dmEl) {
    if (!dms.length) dmEl.innerHTML='<p class="no-data">No conversations yet</p>';
    else dmEl.innerHTML = dms.map(m => {
      const dir = m.direction==='sent'?'▸':'◂';
      const color = m.direction==='sent'?'var(--neon-green)':'var(--neon-cyan)';
      const ts = m.timestamp?m.timestamp.split(' ').pop().substring(0,5):'';
      return `<div class="dm-item"><span class="dm-dir" style="color:${color}">${dir}</span><span class="dm-user">${esc(m.username)} <span style="font-size:10px;color:var(--text3)">${esc(m.platform)}</span></span><span class="dm-content">${esc(m.content)}</span><span class="dm-time">${esc(ts)}</span></div>`;
    }).join('');
  }
  const alEl = document.getElementById('alertList');
  if (alEl) {
    if (!alerts.length) alEl.innerHTML='<p class="no-data">No alerts</p>';
    else alEl.innerHTML = alerts.map(a => {
      const ts = a.timestamp?a.timestamp.split('T').pop().substring(0,5):'';
      return `<div class="feed-item"><span class="time">${esc(ts)}</span><span class="msg">${esc(a.message)}</span></div>`;
    }).join('');
  }
}

// ══════════════════════════════════════════════════════════════
// RENDER: BRAIN
// ══════════════════════════════════════════════════════════════
function renderBrain(d) {
  const el = document.getElementById('brainPanel');
  if (!el) return;
  if (d.error) { el.innerHTML=`<p class="no-data">${esc(d.error)}</p>`; return; }
  let h = '';
  const row = (l,v,c) => `<div class="brain-row"><span class="label">${l}</span><span class="value" style="color:${c||'var(--text)'}">${v}</span></div>`;
  const subs = (d.top_subreddits||[]).slice(0,3).map(s => 'r/'+(s.name||s.subreddit||'?')).join(', ');
  h += row('Top Subreddits', subs||'(learning...)', 'var(--neon-green)');
  h += row('Promo Ratio', Math.round((d.promo_ratio||0.25)*100)+'% promo', 'var(--neon-cyan)');
  h += row('Best Tone', d.best_tone||'N/A', 'var(--text)');
  h += row('Discoveries', (d.discoveries||0)+' pending', d.discoveries>0?'var(--yellow)':'var(--text3)');
  const pt = (d.post_type_top||[]).map(p => p.type+'('+p.avg_eng+')').join(', ');
  h += row('Top Posts', pt||'-', 'var(--neon-cyan)');
  const sent = d.sentiment||{};
  const sIcon = sent.avg>0.1?'▲':sent.avg<-0.1?'▼':'━';
  const sColor = sent.avg>0.1?'var(--neon-green)':sent.avg<-0.1?'var(--red)':'var(--yellow)';
  h += row('Sentiment', `${sIcon} ${(sent.avg||0)>0?'+':''}${(sent.avg||0).toFixed(2)} (${sent.total_replies||0} replies)`, sColor);
  const ab = d.ab_tests||[];
  h += row('A/B Tests', ab.length?ab.map(e=>e.variable+'('+e.a_n+'v'+e.b_n+')').join(' | '):'0 active', ab.length?'var(--neon-purple)':'var(--text3)');
  h += row('Evolved Prompts', (d.evolved_prompts||0)+' templates', d.evolved_prompts>0?'var(--neon-green)':'var(--text3)');
  const llm = d.llm_stats||{};
  if (llm.total_calls!==undefined) {
    h += row('LLM Calls', `${llm.total_calls} (${llm.total_errors||0} err)`, llm.total_errors>0?'var(--red)':'var(--neon-green)');
    if (llm.groq_limit) { const pct=Math.round((llm.groq_rpd||0)/llm.groq_limit*100); h += row('Groq RPD', `${llm.groq_rpd||0}/${llm.groq_limit} (${pct}%)`, pct>80?'var(--red)':pct>50?'var(--yellow)':'var(--neon-green)'); }
    if (llm.creative_chain) h += row('Chain', llm.creative_chain, 'var(--text3)');
    const dis = llm.disabled_providers||{};
    if (Object.keys(dis).length) h += row('Disabled', Object.entries(dis).map(([n,s])=>n+'('+Math.round(s/60)+'m)').join(', '), 'var(--red)');
  }
  const rel = d.relationships||{};
  h += row('Relationships', `${rel.total||0} (${rel.friends||0} friends)`, 'var(--neon-orange)');
  el.innerHTML = h;
}

// ══════════════════════════════════════════════════════════════
// RENDER: PERFORMANCE (with SVG gauge)
// ══════════════════════════════════════════════════════════════
function renderPerformance(d) {
  const el = document.getElementById('perfPanel');
  if (!el) return;
  if (d.error) { el.innerHTML=`<p class="no-data">${esc(d.error)}</p>`; return; }
  let h = drawGauge(d.score, d.grade);
  h += `<div style="text-align:center;font-family:var(--font-data);color:var(--text3);font-size:12px;margin-bottom:14px">${d.total_actions||0} actions today</div>`;
  const comp = d.components||{};
  const max = d.max_per_component||{};
  for (const [k,v] of Object.entries(comp)) {
    const m = max[k]||20;
    const pct = Math.round(v/m*100);
    const color = pct>=80?'var(--neon-green)':pct>=50?'var(--yellow)':'var(--red)';
    h += `<div class="perf-bar-label"><span>${k.charAt(0).toUpperCase()+k.slice(1)}</span><span style="color:${color}">${v}/${m}</span></div>`;
    h += `<div class="perf-bar"><div class="fill" style="width:${pct}%;background:${color}"></div></div>`;
  }
  if (d.improvements&&d.improvements.length) h += `<div class="perf-improvements">Suggestions: ${d.improvements.map(i=>esc(i)).join(' | ')}</div>`;
  else h += `<div style="margin-top:10px;font-size:12px;color:var(--neon-green)">All systems optimal</div>`;
  el.innerHTML = h;
}

// ══════════════════════════════════════════════════════════════
// RENDER: INSIGHTS
// ══════════════════════════════════════════════════════════════
function renderInsights(d) {
  const el = document.getElementById('insightsPanel');
  if (!el) return;
  if (d.error) { el.innerHTML=`<p class="no-data">${esc(d.error)}</p>`; return; }
  let h = '';
  if (d.top_subreddits&&d.top_subreddits.length) {
    h += '<div style="margin-bottom:12px"><div style="font-family:var(--font-label);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px">Top Subreddits</div>';
    const mx = Math.max(...d.top_subreddits.map(s=>s.avg_engagement||s.avg_eng||1));
    d.top_subreddits.slice(0,5).forEach(s => {
      const eng = s.avg_engagement||s.avg_eng||0;
      h += `<div class="insight-bar"><span style="min-width:110px;font-weight:500;font-family:var(--font-label)">${esc(s.subreddit||s.name||'?')}</span><div class="bar"><div class="fill" style="width:${Math.round(eng/mx*100)}%"></div></div><span style="font-family:var(--font-data);font-weight:600">${eng.toFixed(1)}</span></div>`;
    });
    h += '</div>';
  }
  if (d.best_tone) h += `<div style="margin-bottom:10px"><span style="font-family:var(--font-label);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1px">Best Tone: </span><span class="insight-tag">${esc(d.best_tone)}</span></div>`;
  if (d.post_type_stats&&d.post_type_stats.length) {
    h += '<div style="margin-bottom:12px"><div style="font-family:var(--font-label);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px">Post Types</div>';
    d.post_type_stats.slice(0,5).forEach(p => {
      const e = p.avg_engagement||p.avg_eng||0;
      h += `<div class="insight-bar"><span style="min-width:90px;font-weight:500">${esc(p.post_type||'?')}</span><div class="bar"><div class="fill" style="width:${Math.min(100,e*10)}%;background:var(--neon-purple)"></div></div><span style="font-family:var(--font-data);font-weight:600">${e.toFixed(1)}</span></div>`;
    });
    h += '</div>';
  }
  if (d.sentiment&&d.sentiment.length) {
    h += '<div style="margin-bottom:12px"><div style="font-family:var(--font-label);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px">Sentiment by Tone</div>';
    d.sentiment.forEach(s => {
      const sc2 = s.avg_sentiment||s.avg_score||0;
      const c = sc2>0.3?'var(--neon-green)':sc2<-0.3?'var(--red)':'var(--yellow)';
      h += `<div class="insight-bar"><span style="min-width:70px">${esc(s.tone||'?')}</span><div class="bar"><div class="fill" style="width:${Math.min(100,Math.abs(sc2)*100)}%;background:${c}"></div></div><span style="color:${c};font-family:var(--font-data);font-weight:600">${sc2>0?'+':''}${sc2.toFixed(2)}</span></div>`;
    });
    h += '</div>';
  }
  if (d.experiments&&d.experiments.length) {
    h += '<div><div style="font-family:var(--font-label);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:6px">A/B Experiments</div>';
    d.experiments.forEach(e => {
      const aW = e.a_eng>e.b_eng;
      h += `<div style="font-size:11px;margin:4px 0;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:8px"><strong>${esc(e.variable||e.name)}</strong>: <span style="color:${aW?'var(--neon-green)':'var(--text3)'}">${esc(e.variant_a)} (${e.a_eng.toFixed(1)}, n=${e.a_n})</span> vs <span style="color:${!aW?'var(--neon-green)':'var(--text3)'}">${esc(e.variant_b)} (${e.b_eng.toFixed(1)}, n=${e.b_n})</span></div>`;
    });
    h += '</div>';
  }
  if (d.optimal_promo_ratio!==undefined) {
    const pct = Math.round(d.optimal_promo_ratio*100);
    h += `<div style="margin-top:8px"><span style="font-family:var(--font-label);font-size:10px;color:var(--text3);text-transform:uppercase">Promo Ratio: </span><span class="insight-tag">${pct}% promo / ${100-pct}% organic</span></div>`;
  }
  el.innerHTML = h || '<p class="no-data">No insights yet — agent is learning</p>';
}

// ══════════════════════════════════════════════════════════════
// RENDER: OPPORTUNITIES
// ══════════════════════════════════════════════════════════════
function renderOpps(d) {
  const el = document.getElementById('oppsList');
  if (!el) return;
  if (d.error||!d||!d.length) { el.innerHTML='<p class="no-data">No pending opportunities</p>'; return; }
  el.innerHTML = d.slice(0,25).map(o => {
    const sc = o.score||o.relevance_score||0;
    const c = sc>=7?'var(--neon-green)':sc>=4?'var(--yellow)':'var(--text3)';
    return `<div class="feed-item"><span style="color:${c};font-family:var(--font-data);font-weight:700;min-width:32px;font-size:13px">${sc.toFixed(1)}</span><span class="type" style="color:var(--neon-orange)">${esc(o.platform||'')}</span><span class="msg">${esc(o.subreddit_or_query||o.subreddit||'')} — ${esc((o.title||o.content||'').substring(0,80))}</span></div>`;
  }).join('');
}

// ══════════════════════════════════════════════════════════════
// RENDER: DECISION LOG
// ══════════════════════════════════════════════════════════════
function renderDecisionLog(d) {
  const el = document.getElementById('decisionLog');
  const countEl = document.getElementById('decisionCount');
  if (!el) return;
  if (!d||!d.length) { el.innerHTML='<p class="no-data">No recent decisions</p>'; if(countEl)countEl.textContent='0'; return; }
  if (countEl) countEl.textContent = d.length;
  el.innerHTML = d.slice(0,40).map(dec => {
    const ts = dec.timestamp ? dec.timestamp.split(' ').pop().substring(0,5) : '';
    const dtype = (dec.decision_type||dec.type||'').toLowerCase();
    const badgeClass = dtype.includes('select')?'selected':dtype.includes('reject')?'rejected':dtype.includes('rate')?'rate_limited':dtype.includes('dedup')?'dedup':dtype.includes('resource')?'resource_low':'rejected';
    const details = dec.details||dec.reasoning||dec.reason||'';
    const target = dec.target||dec.subreddit||'';
    return `<div class="decision-item"><span class="dec-time">${esc(ts)}</span><span class="dec-badge ${badgeClass}">${esc(dtype)}</span><span class="dec-text">${esc(details).substring(0,120)}</span><span class="dec-target">${esc(target)}</span></div>`;
  }).join('');
}

// ══════════════════════════════════════════════════════════════
// RENDER: HEATMAP
// ══════════════════════════════════════════════════════════════
function renderHeatmap(d) {
  const el = document.getElementById('heatmapContainer');
  if (!el || !d) return;
  const grid = d.grid || [];
  const maxCount = d.max_count || 1;
  const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

  let h = '<div class="heatmap-grid">';
  // Header row (hours)
  h += '<div class="heatmap-label"></div>';
  for (let hr=0; hr<24; hr++) h += `<div class="heatmap-hour">${hr}</div>`;
  // Data rows
  for (let day=0; day<7; day++) {
    h += `<div class="heatmap-label">${days[day]}</div>`;
    for (let hr=0; hr<24; hr++) {
      const cell = grid.find(g => g.dow===day && g.hour===hr);
      const count = cell ? cell.count : 0;
      const intensity = count / maxCount;
      const bg = count === 0 ? 'var(--border)'
        : `rgba(0,240,255,${Math.max(0.1, intensity * 0.8)})`;
      const shadow = intensity > 0.5 ? `box-shadow:0 0 ${Math.round(intensity*8)}px rgba(0,240,255,${intensity*0.3})` : '';
      h += `<div class="heatmap-cell" style="background:${bg};${shadow}"><span class="heatmap-tooltip">${days[day]} ${hr}:00 — ${count} actions</span></div>`;
    }
  }
  h += '</div>';
  el.innerHTML = h;
}

// ══════════════════════════════════════════════════════════════
// RENDER: FUNNEL
// ══════════════════════════════════════════════════════════════
function renderFunnel(d) {
  const el = document.getElementById('funnelContainer');
  if (!el || !d) return;
  const stages = d.stages || [];
  if (!stages.length) { el.innerHTML='<p class="no-data">No funnel data</p>'; return; }
  const maxCount = Math.max(...stages.map(s => s.count), 1);
  const colors = ['#00f0ff','#a855f7','#ff6b35','#22ff88'];
  let h = '';
  stages.forEach((stage, i) => {
    const pct = Math.round(stage.count / maxCount * 100);
    const color = colors[i % colors.length];
    const rate = i > 0 && stages[i-1].count > 0 ? Math.round(stage.count / stages[i-1].count * 100) + '%' : '';
    h += `<div class="funnel-stage">
      <span class="funnel-name">${esc(stage.name)}</span>
      <div class="funnel-bar-wrap"><div class="funnel-bar" style="width:${pct}%;background:${color};opacity:.7"><div class="funnel-bar-inner">${stage.count}</div></div></div>
      <span class="funnel-count" style="color:${color}">${stage.count}</span>
      <span class="funnel-rate">${rate}</span>
    </div>`;
  });
  if (d.conversion_rate !== undefined) {
    h += `<div style="text-align:center;margin-top:8px;font-family:var(--font-data);font-size:12px;color:var(--text3)">End-to-end: <span style="color:var(--neon-cyan);font-weight:700">${(d.conversion_rate*100).toFixed(1)}%</span></div>`;
  }
  el.innerHTML = h;
}

// ══════════════════════════════════════════════════════════════
// RENDER: COMMUNITIES
// ══════════════════════════════════════════════════════════════
function renderCommunities(comms) {
  const el = document.getElementById('commList');
  const countEl = document.getElementById('commCount');
  const statsEl = document.getElementById('commStats');

  if (!comms||!comms.length) {
    if(el) el.innerHTML = '<p class="no-data">No managed communities yet. The agent will create them automatically.</p>';
    if(countEl) countEl.textContent = '0';
    const mrH = document.getElementById('mrHubs'); if(mrH) mrH.textContent = '0';
    return;
  }

  if(countEl) countEl.textContent = comms.length;
  const mrH = document.getElementById('mrHubs'); if(mrH) mrH.textContent = comms.length;
  const setup = comms.filter(c => c.setup_complete).length;
  const active = comms.filter(c => c.post_count > 0).length;

  if(statsEl) statsEl.innerHTML = `
    <div class="stat-card purple"><div class="sv">${comms.length}</div><div class="sl">Total Hubs</div></div>
    <div class="stat-card green"><div class="sv">${setup}</div><div class="sl">Setup Complete</div></div>
    <div class="stat-card cyan"><div class="sv">${active}</div><div class="sl">Active</div></div>
    <div class="stat-card yellow"><div class="sv">${comms.length-setup}</div><div class="sl">Pending</div></div>`;

  if(el) el.innerHTML = comms.map(c => {
    const setupPct = c.setup_complete ? 100 : Math.round(((c.rules_count>0?25:0) + (c.flair_count>0?25:0) + (c.automod_configured?25:0) + (c.sticky_post_1?25:0)));
    const typeColor = c.ownership_type==='created'?'var(--neon-green)':c.ownership_type==='claimed'?'var(--neon-cyan)':'var(--text3)';
    return `<div class="community-card"><div class="comm-header"><span class="comm-name">r/${esc(c.subreddit||c.name)}</span><div style="display:flex;gap:8px;align-items:center"><span class="badge ${c.setup_complete?'on':'warning'}">${c.setup_complete?'Ready':'Setup '+setupPct+'%'}</span><span style="font-size:10px;color:${typeColor};text-transform:uppercase;font-family:var(--font-data)">${esc(c.ownership_type||'pending')}</span></div></div><div class="comm-stats"><span style="color:var(--text3)">${esc(c.project||'')}</span><span>Rules: ${c.rules_count||0}</span><span>Flairs: ${c.flair_count||0}</span><span>Posts: ${c.post_count||0}</span><span>Subs: ${c.subscribers||'?'}</span></div><div class="comm-setup-bar"><div class="fill" style="width:${setupPct}%"></div></div></div>`;
  }).join('');
}

function renderTakeoverTargets(d) {
  const el = document.getElementById('takeoverTargets');
  if (!el) return;
  if (!d||!d.length) { el.innerHTML='<p class="no-data">No takeover targets found</p>'; return; }
  el.innerHTML = d.map(t => {
    const sc = t.takeover_score||t.score||0;
    const c = sc>=8?'var(--neon-green)':sc>=6?'var(--yellow)':'var(--text3)';
    return `<div class="feed-item"><span style="color:${c};font-family:var(--font-data);font-weight:700;min-width:28px">${sc.toFixed(1)}</span><span class="type" style="color:var(--neon-orange)">r/${esc(t.subreddit)}</span><span class="msg">${esc(t.project||'')} — ${esc(t.status||'pending')}</span></div>`;
  }).join('');
}

function renderTakeoverRequests(d) {
  const el = document.getElementById('takeoverRequests');
  if (!el) return;
  if (!d||!d.length) { el.innerHTML='<p class="no-data">No takeover requests submitted</p>'; return; }
  el.innerHTML = d.map(r => {
    const statusColor = {pending:'var(--yellow)',approved:'var(--neon-green)',denied:'var(--red)'}[r.status]||'var(--text3)';
    return `<div class="feed-item"><span class="type" style="color:var(--neon-orange)">r/${esc(r.subreddit)}</span><span class="msg">${esc(r.project)} — ${esc(r.account)}</span><span style="color:${statusColor};font-weight:600;min-width:60px;text-align:right">${esc(r.status)}</span></div>`;
  }).join('');
}

// ══════════════════════════════════════════════════════════════
// RENDER: SERVER
// ══════════════════════════════════════════════════════════════
function renderServer(d) {
  if (!d||d.error) return;
  const cpu=d.cpu||{}, ram=d.ram||{}, disk=d.disk||{}, proc=d.process||{}, db=d.database||{};

  const mrCPU = document.getElementById('mrCPU');
  const mrRAM = document.getElementById('mrRAM');
  if(mrCPU) { mrCPU.textContent = (cpu.usage_pct||0)+'%'; mrCPU.style.color = cpu.usage_pct>60?'var(--red)':cpu.usage_pct>30?'var(--yellow)':'var(--neon-green)'; }
  if(mrRAM) { mrRAM.textContent = (proc.rss_mb||0)+'MB'; mrRAM.style.color = proc.rss_mb>300?'var(--red)':proc.rss_mb>200?'var(--yellow)':'var(--neon-green)'; }

  const ss = document.getElementById('serverStats');
  if(ss) ss.innerHTML = `
    <div class="stat-card ${cpu.usage_pct>60?'red':cpu.usage_pct>30?'yellow':'green'}"><div class="sv">${cpu.usage_pct||0}%</div><div class="sl">CPU (${cpu.cores||'?'}c)</div></div>
    <div class="stat-card ${ram.percent>85?'red':ram.percent>70?'yellow':'green'}"><div class="sv">${ram.percent||0}%</div><div class="sl">RAM ${ram.used_gb||0}/${ram.total_gb||0}G</div></div>
    <div class="stat-card ${disk.percent>90?'red':disk.percent>80?'yellow':'green'}"><div class="sv">${disk.percent||0}%</div><div class="sl">Disk ${disk.free_gb||0}G free</div></div>
    <div class="stat-card blue"><div class="sv">${proc.rss_mb||0}</div><div class="sl">RSS MB</div></div>
    <div class="stat-card purple"><div class="sv">${proc.threads||0}</div><div class="sl">Threads</div></div>
    <div class="stat-card cyan"><div class="sv">${db.size_mb||0}</div><div class="sl">DB Size MB</div></div>`;

  const sb = document.getElementById('serverBars');
  if(sb) sb.innerHTML = resBar('CPU',cpu.usage_pct||0,50,80)+resBar('RAM',ram.percent||0,70,90)+resBar('Disk',disk.percent||0,80,95)+resBar('Bot',Math.min(100,Math.round((proc.rss_mb||0)/4)),50,80);

  const hist = d.history||[];
  if (hist.length>2) {
    const labels = hist.map(h=>h.ts);
    const cpuData = hist.map(h=>h.cpu);
    const ramData = hist.map(h=>h.ram);
    if (charts.resources) {
      charts.resources.data.labels=labels; charts.resources.data.datasets[0].data=cpuData; charts.resources.data.datasets[1].data=ramData; charts.resources.update();
    } else {
      const ctx = document.getElementById('chartResources');
      if (!ctx) return;
      const context = ctx.getContext('2d');
      const gradC = context.createLinearGradient(0,0,0,220);
      gradC.addColorStop(0,'rgba(59,130,246,.12)'); gradC.addColorStop(1,'rgba(59,130,246,0)');
      const gradR = context.createLinearGradient(0,0,0,220);
      gradR.addColorStop(0,'rgba(168,85,247,.12)'); gradR.addColorStop(1,'rgba(168,85,247,0)');
      charts.resources = new Chart(context, {
        type:'line', data:{labels, datasets:[
          {label:'CPU %',data:cpuData,borderColor:'#3b82f6',backgroundColor:gradC,fill:true,tension:.3,pointRadius:0,borderWidth:2},
          {label:'RAM %',data:ramData,borderColor:'#a855f7',backgroundColor:gradR,fill:true,tension:.3,pointRadius:0,borderWidth:2}
        ]},
        options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
          plugins:{legend:{labels:{color:'#7a8ba8',font:{size:11,family:'Exo 2'},usePointStyle:true}},
            tooltip:{backgroundColor:'rgba(10,15,30,.9)',borderColor:'rgba(0,240,255,.2)',borderWidth:1,padding:10,cornerRadius:8}
          },scales:{x:{ticks:{color:'#4a5b73',font:{size:10,family:'JetBrains Mono'},maxTicksLimit:10},grid:{color:'rgba(26,39,68,.4)'}},y:{min:0,max:100,ticks:{color:'#4a5b73'},grid:{color:'rgba(26,39,68,.4)'}}}}
      });
    }
  }
}

// ══════════════════════════════════════════════════════════════
// RENDER: MANAGE
// ══════════════════════════════════════════════════════════════
function renderManageProjects(d) {
  const el = document.getElementById('projectsManage');
  if (!el) return;
  if (!d||!d.length) { el.innerHTML='<p class="no-data">No projects</p>'; return; }
  el.innerHTML = d.map(p => `<div class="entity-card"><div><div class="name">${esc(p.name)} <span class="badge ${p.enabled?'on':'off'}">${p.enabled?'Active':'Off'}</span></div><div class="meta">${esc(p.url||'')} — ${p.actions_24h||0} actions/24h — weight: ${p.weight||1}</div></div><div class="actions-area"><button class="btn btn-sm" onclick="editProject(${JSON.stringify(p.name)})">Edit</button><button class="btn btn-sm danger" onclick="deleteProject(${JSON.stringify(p.name)})">Delete</button></div></div>`).join('');
}

function renderManageAccounts(d) {
  const el = document.getElementById('accountsManage');
  if (!el) return;
  if (!d||!d.length) { el.innerHTML='<p class="no-data">No accounts</p>'; return; }
  const filtered = d.filter(a => a.platform !== 'twitter');
  el.innerHTML = filtered.map(a => `<div class="entity-card"><div><div class="name">@${esc(a.username)} <span style="font-family:var(--font-data);font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px">${esc(a.platform)}</span></div><div class="meta">${a.total_24h||0} actions — C:${a.comments||0} L:${a.likes||0} P:${a.posts||0} — ${esc(a.persona||'default')}</div></div><div class="actions-area">${hpBar(a.status)}<span class="badge ${a.status==='healthy'?'healthy':a.status==='cooldown'?'cooldown':'error'}">${esc(a.status)}</span><button class="btn btn-sm danger" onclick="removeAccount(${JSON.stringify(a.platform)},${JSON.stringify(a.username)})">Remove</button></div></div>`).join('');
}

// ══════════════════════════════════════════════════════════════
// RENDER: NETWORK GRAPH (D3.js)
// ══════════════════════════════════════════════════════════════
function loadD3() {
  return new Promise((resolve) => {
    if (window.d3) { _d3Loaded = true; return resolve(); }
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js';
    s.onload = () => { _d3Loaded = true; resolve(); };
    s.onerror = () => resolve(); // Fail gracefully
    document.head.appendChild(s);
  });
}

async function renderNetwork(data) {
  const container = document.getElementById('networkGraph');
  if (!container || !data) return;
  const nodeCountEl = document.getElementById('networkNodeCount');
  if (nodeCountEl) nodeCountEl.textContent = (data.nodes||[]).length;

  if (!_d3Loaded) {
    container.innerHTML = '<p class="no-data">Loading D3.js...</p>';
    await loadD3();
    if (!window.d3) { container.innerHTML = '<p class="no-data">Could not load D3.js</p>'; return; }
  }

  const nodes = data.nodes || [];
  const links = data.links || [];
  if (!nodes.length) { container.innerHTML = '<p class="no-data">No network data</p>'; return; }

  container.innerHTML = '';
  const width = container.clientWidth || 800;
  const height = container.clientHeight || 550;

  const svg = d3.select(container).append('svg')
    .attr('width', width).attr('height', height);

  // Glow filter
  const defs = svg.append('defs');
  const filter = defs.append('filter').attr('id', 'glow');
  filter.append('feGaussianBlur').attr('stdDeviation', '3').attr('result', 'blur');
  const merge = filter.append('feMerge');
  merge.append('feMergeNode').attr('in', 'blur');
  merge.append('feMergeNode').attr('in', 'SourceGraphic');

  const g = svg.append('g');
  // Zoom
  svg.call(d3.zoom().scaleExtent([0.3, 5]).on('zoom', (e) => g.attr('transform', e.transform)));

  const colorMap = {account:'#00f0ff', subreddit:'#ff6b35', relationship:'#a855f7'};
  const stageColorMap = {noticed:'#4a5b73',engaged:'#eab308',warm:'#ff6b35',friend:'#22ff88',advocate:'#00f0ff'};
  const nodeColor = (n) => n.type === 'relationship' ? (stageColorMap[n.stage]||'#a855f7') : (colorMap[n.type]||'#7a8ba8');
  const nodeRadius = (n) => n.type === 'account' ? 10 : n.type === 'subreddit' ? 8 : 6;

  const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(80).strength(0.5))
    .force('charge', d3.forceManyBody().strength(-180))
    .force('center', d3.forceCenter(width/2, height/2))
    .force('collision', d3.forceCollide().radius(d => nodeRadius(d) + 4));

  const link = g.append('g').selectAll('line').data(links).join('line')
    .attr('stroke', '#1a2744').attr('stroke-opacity', 0.6)
    .attr('stroke-width', d => Math.max(1, Math.min(4, d.value||1)));

  const node = g.append('g').selectAll('circle').data(nodes).join('circle')
    .attr('r', d => nodeRadius(d))
    .attr('fill', d => nodeColor(d))
    .attr('stroke', d => nodeColor(d))
    .attr('stroke-width', 1.5)
    .attr('stroke-opacity', 0.4)
    .attr('filter', 'url(#glow)')
    .style('cursor', 'pointer')
    .call(d3.drag().on('start', dragStarted).on('drag', dragged).on('end', dragEnded));

  const label = g.append('g').selectAll('text').data(nodes).join('text')
    .text(d => d.label)
    .attr('font-size', 9).attr('fill', '#7a8ba8').attr('font-family', 'JetBrains Mono, monospace')
    .attr('dx', 14).attr('dy', 4);

  // Tooltip
  const tooltip = d3.select(container).append('div').attr('class','network-tooltip').style('display','none');
  node.on('mouseover', function(e, d) {
    tooltip.style('display','block')
      .html(`<strong>${esc(d.label)}</strong><br>Type: ${d.type}${d.stage ? '<br>Stage: '+d.stage : ''}${d.activity ? '<br>Activity: '+d.activity : ''}${d.trust ? '<br>Trust: '+d.trust.toFixed(2) : ''}`)
      .style('left', (e.offsetX+15)+'px').style('top', (e.offsetY-10)+'px');
    d3.select(this).attr('r', nodeRadius(d)*1.5);
  }).on('mouseout', function(e, d) {
    tooltip.style('display','none');
    d3.select(this).attr('r', nodeRadius(d));
  });

  simulation.on('tick', () => {
    link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
    node.attr('cx',d=>d.x).attr('cy',d=>d.y);
    label.attr('x',d=>d.x).attr('y',d=>d.y);
  });

  _networkSim = simulation;

  function dragStarted(e,d) { if(!e.active) simulation.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; }
  function dragged(e,d) { d.fx=e.x; d.fy=e.y; }
  function dragEnded(e,d) { if(!e.active) simulation.alphaTarget(0); d.fx=null; d.fy=null; }
}

function filterNetwork(type) {
  // Re-fetch and filter will happen on next refresh
  document.querySelectorAll('.network-controls .btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  // For now, just re-trigger refresh for the network tab
  if (currentTab === 'network') refresh();
}

// ══════════════════════════════════════════════════════════════
// CONTROLS
// ══════════════════════════════════════════════════════════════
async function doControl(action) {
  try {
    const d = await apiPost('/api/control/'+action);
    if (d&&!d.ok) { toast(d.error||'Failed','error'); return; }
    if (action==='pause') paused=true;
    if (action==='resume') paused=false;
    const bp = document.getElementById('btnPause');
    const br = document.getElementById('btnResume');
    if(bp) bp.disabled=paused;
    if(br) br.disabled=!paused;
    toast(action.charAt(0).toUpperCase()+action.slice(1)+' executed','success');
  } catch(e) { toast('Error: '+e.message,'error'); }
}
async function emergencyStop() {
  if (!confirm('EMERGENCY STOP — Freeze ALL operations?')) return;
  try { await apiPost('/api/control/emergency-stop'); toast('EMERGENCY STOP ACTIVATED','error'); refresh(); } catch(e) { toast(e.message,'error'); }
}
async function emergencyReset() {
  try { await apiPost('/api/control/emergency-reset'); toast('Emergency reset OK','success'); refresh(); } catch(e) { toast(e.message,'error'); }
}

// ══════════════════════════════════════════════════════════════
// CRUD: PROJECTS
// ══════════════════════════════════════════════════════════════
async function submitProject() {
  const name = document.getElementById('pName').value.trim();
  const url = document.getElementById('pUrl').value.trim();
  const desc = document.getElementById('pDesc').value.trim();
  if (!name||!url||!desc) { toast('Name, URL and Description required','error'); return; }
  try {
    const d = await apiPost('/api/projects', {
      name, url, description:desc, project_type:document.getElementById('pType').value,
      weight:parseFloat(document.getElementById('pWeight').value)||1.0,
      tagline:document.getElementById('pTagline').value.trim(),
      selling_points:splitCSV(document.getElementById('pSelling').value),
      target_audiences:splitCSV(document.getElementById('pAudiences').value),
    });
    if (d.ok) { toast('Project created','success'); closeModal(); refresh(); }
    else toast(d.detail||'Failed','error');
  } catch(e) { toast(e.message,'error'); }
}

async function editProject(name) {
  _editingProject = name;
  try {
    const d = await api('/api/projects/'+encodeURIComponent(name));
    const proj = d.project||{};
    document.getElementById('editProjName').textContent = name;
    document.getElementById('epEnabled').value = proj.enabled!==false?'true':'false';
    document.getElementById('epWeight').value = proj.weight||1.0;
    document.getElementById('epUrl').value = proj.url||'';
    document.getElementById('epDesc').value = proj.description||'';
    document.getElementById('epTagline').value = proj.tagline||'';
    document.getElementById('epTone').value = (d.tone||{}).style||'helpful_casual';
    const reddit = d.reddit||{};
    const subs = reddit.target_subreddits||{};
    document.getElementById('epSubsPrimary').value = (subs.primary||[]).join(', ');
    document.getElementById('epSubsSecondary').value = (subs.secondary||[]).join(', ');
    document.getElementById('epRedditKw').value = (reddit.keywords||[]).join(', ');
    openModal('editProject');
  } catch(e) { toast('Error loading project','error'); }
}

async function submitEditProject() {
  const body = {
    enabled:document.getElementById('epEnabled').value==='true',
    weight:parseFloat(document.getElementById('epWeight').value)||1.0,
    url:document.getElementById('epUrl').value.trim()||null,
    description:document.getElementById('epDesc').value.trim()||null,
    tagline:document.getElementById('epTagline').value.trim()||null,
    tone_style:document.getElementById('epTone').value||null,
    reddit_subreddits_primary:splitCSV(document.getElementById('epSubsPrimary').value),
    reddit_subreddits_secondary:splitCSV(document.getElementById('epSubsSecondary').value),
    reddit_keywords:splitCSV(document.getElementById('epRedditKw').value),
  };
  try {
    const d = await apiPut('/api/projects/'+encodeURIComponent(_editingProject), body);
    if (d.ok) { toast('Project updated','success'); closeModal(); refresh(); }
    else toast(d.detail||'Failed','error');
  } catch(e) { toast(e.message,'error'); }
}

async function deleteProject(name) {
  if (!confirm(`Delete project "${name}"?`)) return;
  try { const d=await apiDelete('/api/projects/'+encodeURIComponent(name)); if(d.ok){toast('Deleted','success');refresh()}else toast(d.detail||'Failed','error'); } catch(e){toast(e.message,'error')}
}

// ══════════════════════════════════════════════════════════════
// CRUD: ACCOUNTS
// ══════════════════════════════════════════════════════════════
async function submitAccount() {
  const platform=document.getElementById('aPlat').value, username=document.getElementById('aUser').value.trim(), password=document.getElementById('aPass').value.trim();
  if (!username||!password) { toast('Username and Password required','error'); return; }
  try {
    const d = await apiPost('/api/accounts', {platform,username,password,email:document.getElementById('aEmail').value.trim(),persona:document.getElementById('aPersona').value});
    toast(d.message||'Done', d.ok?'success':'error');
    if (d.ok) { closeModal(); refresh(); }
  } catch(e) { toast(e.message,'error'); }
}
async function removeAccount(platform, username) {
  if (!confirm(`Remove @${username} (${platform})?`)) return;
  try { const d=await apiDelete(`/api/accounts/${platform}/${encodeURIComponent(username)}`); toast(d.message||'Done',d.ok?'success':'error'); refresh(); } catch(e){toast(e.message,'error')}
}

// ══════════════════════════════════════════════════════════════
// COOKIES
// ══════════════════════════════════════════════════════════════
function renderCookies(d) {
  const el = document.getElementById('cookiesStatus');
  if (!el) return;
  if (!d||!d.length) { el.innerHTML='<p class="no-data">No accounts configured</p>'; return; }
  const filtered = d.filter(c => c.platform !== 'twitter');
  el.innerHTML = filtered.map(c => {
    const ok = c.has_cookies;
    const keys = (c.key_cookies||[]).join(', ');
    return `<div class="entity-card"><div><div class="name">@${esc(c.username)} <span style="font-family:var(--font-data);font-size:10px;color:var(--text3);text-transform:uppercase">${esc(c.platform)}</span></div><div class="meta">${ok ? `${c.count||'?'} cookies | Keys: ${keys||'none'} | ${c.size_kb||0}KB` : '<span style="color:var(--red)">No cookies — login required</span>'}</div></div><div class="actions-area"><span class="badge ${ok?'on':'off'}">${ok?'Active':'Missing'}</span>${ok?`<button class="btn btn-sm danger" onclick="deleteCookies(${JSON.stringify(c.platform)},${JSON.stringify(c.username)})">Delete</button>`:''}</div></div>`;
  }).join('');
}

async function loadCookieAccounts() {
  const plat = document.getElementById('cookiePlat').value;
  const sel = document.getElementById('cookieAccount');
  if (!sel) return;
  sel.innerHTML = '';
  const cached = _cookieAccounts[plat];
  if (cached) { cached.forEach(a => { const o=document.createElement('option'); o.value=a; o.textContent='@'+a; sel.appendChild(o); }); return; }
  try {
    const d = await api('/api/cookies');
    const accs = d.filter(c=>c.platform===plat).map(c=>c.username);
    _cookieAccounts[plat] = accs;
    accs.forEach(a => { const o=document.createElement('option'); o.value=a; o.textContent='@'+a; sel.appendChild(o); });
  } catch(e) { sel.innerHTML='<option>Error</option>'; }
}

async function submitPasteCookies() {
  const platform=document.getElementById('cookiePlat').value, username=document.getElementById('cookieAccount').value, raw=document.getElementById('cookieRaw').value.trim();
  if (!username||!raw) { toast('Select account and paste cookies','error'); return; }
  try {
    const d = await apiPost('/api/cookies/paste', {platform,username,cookies:raw});
    if (d.ok) {
      let msg = d.message;
      if (d.key_cookies_found&&d.key_cookies_found.length) msg += ` | Keys: ${d.key_cookies_found.join(', ')}`;
      toast(msg, 'success'); closeModal(); refresh();
    } else toast(d.detail||'Failed','error');
  } catch(e) { toast(e.message,'error'); }
}

async function deleteCookies(platform, username) {
  if (!confirm(`Delete cookies for @${username}?`)) return;
  try { const d=await apiDelete(`/api/cookies/${platform}/${encodeURIComponent(username)}`); toast(d.message||'Done',d.ok?'success':'error'); _cookieAccounts={}; refresh(); } catch(e){toast(e.message,'error')}
}

// ══════════════════════════════════════════════════════════════
// MODALS
// ══════════════════════════════════════════════════════════════
function openModal(id) { const m=document.getElementById('modal-'+id); if(m)m.classList.add('show'); if(id==='pasteCookies')loadCookieAccounts(); }
function closeModal() { document.querySelectorAll('.modal-overlay').forEach(m=>m.classList.remove('show')); }

// ══════════════════════════════════════════════════════════════
// WEBSOCKET LOGS
// ══════════════════════════════════════════════════════════════
function connectWS() {
  if (ws) { ws.close(); ws=null; }
  if (!TOKEN) return;
  const proto = location.protocol==='https:'?'wss:':'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/logs?token=${encodeURIComponent(TOKEN)}`);
  ws.onmessage = (e) => {
    const rec = JSON.parse(e.data);
    _logCount++;
    const lc = document.getElementById('logCount');
    if (lc) lc.textContent = _logCount;

    // Logs panel (server tab)
    const panel = document.getElementById('logsPanel');
    if (panel) {
      const div = document.createElement('div');
      div.className = 'feed-item';
      const levelColor = {ERROR:'var(--red)',WARNING:'var(--yellow)',INFO:'var(--neon-green)',DEBUG:'var(--text3)'}[rec.level]||'var(--text2)';
      div.innerHTML = `<span class="time">${esc(rec.ts)}</span><span class="type" style="color:${levelColor}">${esc(rec.level)}</span><span class="msg">${esc(rec.msg)}</span>`;
      panel.appendChild(div);
      while (panel.children.length>200) panel.removeChild(panel.firstChild);
      panel.scrollTop = panel.scrollHeight;
    }

    // Chat feed (activity tab)
    const chat = document.getElementById('chatFeed');
    if (chat) {
      const cm = document.createElement('div');
      cm.className = 'chat-msg';
      const cat = rec.cat||'';
      cm.innerHTML = `<span class="chat-ts">${esc(rec.ts)}</span>${cat?`<span class="cat-badge ${cat}">${cat}</span>`:''}<span class="chat-text">${esc(rec.msg)}</span>`;
      chat.appendChild(cm);
      while (chat.children.length>250) chat.removeChild(chat.firstChild);
      chat.scrollTop = chat.scrollHeight;
    }

    // Toast for errors
    if (rec.level === 'ERROR' && rec.cat === 'ERR') {
      toast(rec.msg.substring(0,80), 'error');
    }
  };
  ws.onclose = () => { if(TOKEN) setTimeout(connectWS, 3000); };
  ws.onerror = () => { ws.close(); };
}

// ══════════════════════════════════════════════════════════════
// PARTICLE SYSTEM
// ══════════════════════════════════════════════════════════════
function initParticles() {
  const canvas = document.getElementById('particleCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let particles = [];
  const count = 40;

  function resize() { canvas.width = window.innerWidth; canvas.height = window.innerHeight; }
  resize();
  window.addEventListener('resize', resize);

  for (let i = 0; i < count; i++) {
    particles.push({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      vx: (Math.random() - 0.5) * 0.3,
      vy: (Math.random() - 0.5) * 0.3,
      r: Math.random() * 1.5 + 0.5,
    });
  }

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // Draw connections
    for (let i = 0; i < particles.length; i++) {
      for (let j = i + 1; j < particles.length; j++) {
        const dx = particles[i].x - particles[j].x;
        const dy = particles[i].y - particles[j].y;
        const dist = Math.sqrt(dx*dx + dy*dy);
        if (dist < 120) {
          ctx.beginPath();
          ctx.moveTo(particles[i].x, particles[i].y);
          ctx.lineTo(particles[j].x, particles[j].y);
          ctx.strokeStyle = `rgba(0,240,255,${0.06 * (1 - dist/120)})`;
          ctx.lineWidth = 0.5;
          ctx.stroke();
        }
      }
    }
    // Draw particles
    particles.forEach(p => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(0,240,255,0.2)';
      ctx.fill();
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > canvas.width) p.vx *= -1;
      if (p.y < 0 || p.y > canvas.height) p.vy *= -1;
    });
    _particleAnim = requestAnimationFrame(draw);
  }
  draw();
}

// Pause when tab hidden
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (_particleAnim) { cancelAnimationFrame(_particleAnim); _particleAnim = null; }
  } else {
    if (!_particleAnim) initParticles();
  }
});

// ══════════════════════════════════════════════════════════════
// REFRESH LOOP
// ══════════════════════════════════════════════════════════════
async function refresh() {
  if (_refreshing) return;
  _refreshing = true;
  try {
    const status = await api('/api/status').catch(()=>null);
    if (status) renderStatus(status);

    if (currentTab === 'command') {
      const [stats, minimaps, schedule, history, acctPerf, heatmap, funnel] = await Promise.allSettled([
        api('/api/stats'), api('/api/minimaps'), api('/api/schedule'), api('/api/history?hours=168'),
        api('/api/accounts/reddit/performance'),
        api('/api/heatmap').catch(()=>null),
        api('/api/funnel').catch(()=>null)
      ]);
      if (stats.status==='fulfilled') renderStats(stats.value);
      if (minimaps.status==='fulfilled') renderMinimaps(minimaps.value);
      if (schedule.status==='fulfilled') renderSchedule(schedule.value, 'scheduleList');
      if (history.status==='fulfilled') renderTimeline(history.value);
      if (acctPerf.status==='fulfilled') renderRedditAcctPerf(acctPerf.value);
      if (heatmap.status==='fulfilled' && heatmap.value) renderHeatmap(heatmap.value);
      if (funnel.status==='fulfilled' && funnel.value) renderFunnel(funnel.value);
    }
    else if (currentTab === 'activity') {
      const [actions, convos] = await Promise.allSettled([api('/api/actions?limit=50'), api('/api/conversations')]);
      if (actions.status==='fulfilled') renderActions(actions.value);
      if (convos.status==='fulfilled') renderConversations(convos.value);
    }
    else if (currentTab === 'intel') {
      const [brain, perf, insights, opps, decisions] = await Promise.allSettled([
        api('/api/brain'), api('/api/performance'), api('/api/insights'), api('/api/opportunities?limit=25'),
        api('/api/decisions?hours=4&limit=40').catch(()=>[])
      ]);
      if (brain.status==='fulfilled') renderBrain(brain.value);
      if (perf.status==='fulfilled') renderPerformance(perf.value);
      if (insights.status==='fulfilled') renderInsights(insights.value);
      if (opps.status==='fulfilled') renderOpps(opps.value);
      if (decisions.status==='fulfilled') renderDecisionLog(decisions.value);
    }
    else if (currentTab === 'communities') {
      const [comms, targets, requests] = await Promise.allSettled([
        api('/api/communities'), api('/api/takeover/targets'), api('/api/takeover/requests')
      ]);
      if (comms.status==='fulfilled') renderCommunities(comms.value);
      if (targets.status==='fulfilled') renderTakeoverTargets(targets.value);
      if (requests.status==='fulfilled') renderTakeoverRequests(requests.value);
    }
    else if (currentTab === 'manage') {
      const [projects, accounts, cookies] = await Promise.allSettled([
        api('/api/projects'), api('/api/accounts'), api('/api/cookies')
      ]);
      if (projects.status==='fulfilled') renderManageProjects(projects.value);
      if (accounts.status==='fulfilled') {
        renderManageAccounts(accounts.value);
        const mrAcc = document.getElementById('mrAccounts');
        if (mrAcc) mrAcc.textContent = accounts.value.filter(a=>a.platform!=='twitter').length;
      }
      if (cookies.status==='fulfilled') renderCookies(cookies.value);
    }
    else if (currentTab === 'server') {
      const [server, schedule] = await Promise.allSettled([api('/api/server'), api('/api/schedule')]);
      if (server.status==='fulfilled') renderServer(server.value);
      if (schedule.status==='fulfilled') renderSchedule(schedule.value, 'scheduleListFull');
    }
    else if (currentTab === 'network') {
      const data = await api('/api/network').catch(()=>null);
      if (data) renderNetwork(data);
    }
  } catch(e) { console.error('Refresh error:', e); }
  _refreshing = false;
}

// ══════════════════════════════════════════════════════════════
// START + BOOT
// ══════════════════════════════════════════════════════════════
function startDashboard() {
  refresh();
  refreshTimer = setInterval(refresh, 5000);
  connectWS();
  initParticles();
}

async function boot() {
  if (!TOKEN) { showLogin(); return; }
  try {
    const r = await fetch('/api/status', {headers:{'Authorization':'Bearer '+TOKEN}});
    if (r.ok) showDashboard();
    else { TOKEN=''; localStorage.removeItem('milo_token'); showLogin(); }
  } catch(e) { showDashboard(); }
}
boot();
