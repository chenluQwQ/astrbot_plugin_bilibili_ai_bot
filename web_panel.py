"""
BiliBot Web Panel - 管理面板模块
提供 Web 界面查看状态、管理记忆、好感度、动态日志等
"""
import os
import json
import asyncio
import hashlib
from datetime import datetime
from aiohttp import web
from astrbot.api import logger

class WebPanel:
    def __init__(self, plugin, port=5001, password="admin123"):
        self.plugin = plugin
        self.port = port
        self.password_hash = hashlib.sha256(password.encode()).hexdigest()
        self.app = None
        self.runner = None
        self.sessions = set()
    
    def _check_auth(self, request):
        token = request.cookies.get('bili_token', '')
        return token in self.sessions
    
    def _json_response(self, data, status=200):
        return web.json_response(data, status=status)
    
    async def start(self):
        self.app = web.Application()
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_post('/api/login', self.handle_login)
        self.app.router.add_get('/api/auth_check', self.handle_auth_check)
        self.app.router.add_get('/api/health', self.handle_health)
        self.app.router.add_get('/api/status', self.handle_status)
        self.app.router.add_get('/api/memory/list', self.handle_memory_list)
        self.app.router.add_post('/api/memory/delete', self.handle_memory_delete)
        self.app.router.add_get('/api/affection/list', self.handle_affection_list)
        self.app.router.add_get('/api/permanent/list', self.handle_permanent_list)
        self.app.router.add_post('/api/permanent/add', self.handle_permanent_add)
        self.app.router.add_post('/api/permanent/delete', self.handle_permanent_delete)
        self.app.router.add_get('/api/personality', self.handle_personality)
        self.app.router.add_get('/api/dynamic/list', self.handle_dynamic_list)
        self.app.router.add_get('/api/export', self.handle_export)
        
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await site.start()
        logger.info(f"[BiliBot] 🌐 Web面板启动: http://0.0.0.0:{self.port}")
    
    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            logger.info("[BiliBot] 🌐 Web面板已停止")
    
    # ===== 页面 =====
    async def handle_index(self, request):
        return web.Response(text=self._get_html(), content_type='text/html')
    
    # ===== API =====
    async def handle_login(self, request):
        try:
            data = await request.json()
            pwd = data.get('password', '')
            if hashlib.sha256(pwd.encode()).hexdigest() == self.password_hash:
                import secrets
                token = secrets.token_hex(16)
                self.sessions.add(token)
                resp = self._json_response({'ok': True})
                resp.set_cookie('bili_token', token, max_age=86400)
                return resp
            return self._json_response({'ok': False, 'error': '密码错误'}, 401)
        except:
            return self._json_response({'ok': False, 'error': '请求错误'}, 400)
    
    async def handle_auth_check(self, request):
        return self._json_response({'ok': self._check_auth(request)})
    
    async def handle_health(self, request):
        return self._json_response({'ok': True, 'running': self.plugin._running})
    
    async def handle_status(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        p = self.plugin
        from . import MEMORY_FILE, PERMANENT_MEMORY_FILE, USER_PROFILE_FILE, PERSONALITY_FILE, WATCH_LOG_FILE, DYNAMIC_LOG_FILE
        memory = p._load_json(MEMORY_FILE, [])
        perm = p._load_json(PERMANENT_MEMORY_FILE, [])
        profiles = p._load_json(USER_PROFILE_FILE, {})
        evo = p._load_json(PERSONALITY_FILE, {})
        wl = p._load_json(WATCH_LOG_FILE, [])
        dl = p._load_json(DYNAMIC_LOG_FILE, [])
        today = datetime.now().strftime("%Y-%m-%d")
        return self._json_response({
            'running': p._running,
            'memory_count': len(memory),
            'permanent_count': len(perm),
            'profile_count': len(profiles),
            'affection_count': len(p._affection),
            'personality_version': evo.get('version', 0),
            'today_watched': len([l for l in wl if l.get('time', '').startswith(today)]),
            'today_dynamic': len([l for l in dl if l.get('time', '').startswith(today)]),
            'features': {
                'reply': p.config.get('ENABLE_REPLY', True),
                'affection': p.config.get('ENABLE_AFFECTION', True),
                'mood': p.config.get('ENABLE_MOOD', True),
                'proactive': p.config.get('ENABLE_PROACTIVE', False),
                'dynamic': p.config.get('ENABLE_DYNAMIC', False),
                'evolution': p.config.get('ENABLE_PERSONALITY_EVOLUTION', True),
            }
        })
    
    async def handle_memory_list(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        from . import MEMORY_FILE
        memory = self.plugin._load_json(MEMORY_FILE, [])
        page = int(request.query.get('page', 1))
        per_page = 20
        start = (page - 1) * per_page
        items = memory[-(start + per_page):len(memory) - start] if start < len(memory) else []
        items.reverse()
        return self._json_response({
            'items': [{'id': m.get('rpid', ''), 'text': m.get('text', '')[:200], 'time': m.get('time', ''), 'source': m.get('source', 'bilibili')} for m in items],
            'total': len(memory),
            'page': page,
            'pages': (len(memory) + per_page - 1) // per_page
        })
    
    async def handle_memory_delete(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        try:
            data = await request.json()
            rpid = data.get('rpid', '')
            from . import MEMORY_FILE
            memory = self.plugin._load_json(MEMORY_FILE, [])
            memory = [m for m in memory if m.get('rpid') != rpid]
            self.plugin._save_json(MEMORY_FILE, memory)
            self.plugin._memory = memory
            return self._json_response({'ok': True})
        except:
            return self._json_response({'ok': False}, 400)
    
    async def handle_affection_list(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        aff = self.plugin._affection
        items = sorted([{'uid': k, 'score': v} for k, v in aff.items()], key=lambda x: x['score'], reverse=True)
        return self._json_response({'items': items[:50], 'total': len(items)})
    
    async def handle_permanent_list(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        from . import PERMANENT_MEMORY_FILE
        perm = self.plugin._load_json(PERMANENT_MEMORY_FILE, [])
        return self._json_response({'items': perm})
    
    async def handle_permanent_add(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        try:
            data = await request.json()
            text = data.get('text', '').strip()
            if not text:
                return self._json_response({'ok': False, 'error': '内容不能为空'}, 400)
            from . import PERMANENT_MEMORY_FILE
            perm = self.plugin._load_json(PERMANENT_MEMORY_FILE, [])
            perm.append({'text': text, 'time': datetime.now().strftime('%Y-%m-%d %H:%M'), 'source': 'web_panel'})
            self.plugin._save_json(PERMANENT_MEMORY_FILE, perm)
            return self._json_response({'ok': True})
        except:
            return self._json_response({'ok': False}, 400)
    
    async def handle_permanent_delete(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        try:
            data = await request.json()
            index = data.get('index', -1)
            from . import PERMANENT_MEMORY_FILE
            perm = self.plugin._load_json(PERMANENT_MEMORY_FILE, [])
            if 0 <= index < len(perm):
                perm.pop(index)
                self.plugin._save_json(PERMANENT_MEMORY_FILE, perm)
                return self._json_response({'ok': True})
            return self._json_response({'ok': False, 'error': '索引无效'}, 400)
        except:
            return self._json_response({'ok': False}, 400)
    
    async def handle_personality(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        from . import PERSONALITY_FILE
        evo = self.plugin._load_json(PERSONALITY_FILE, {})
        return self._json_response({
            'version': evo.get('version', 0),
            'last_evolve': evo.get('last_evolve', '从未'),
            'traits': evo.get('traits', []),
            'speech_habits': evo.get('speech_habits', []),
            'opinions': evo.get('opinions', []),
            'last_reflection': evo.get('last_reflection', '')
        })
    
    async def handle_dynamic_list(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        from . import DYNAMIC_LOG_FILE
        log = self.plugin._load_json(DYNAMIC_LOG_FILE, [])
        return self._json_response({'items': log[-50:]})
    
    async def handle_export(self, request):
        if not self._check_auth(request):
            return self._json_response({'error': '未登录'}, 401)
        from . import MEMORY_FILE, PERMANENT_MEMORY_FILE, USER_PROFILE_FILE, AFFECTION_FILE, PERSONALITY_FILE, DYNAMIC_LOG_FILE
        return self._json_response({
            'memory': self.plugin._load_json(MEMORY_FILE, []),
            'permanent': self.plugin._load_json(PERMANENT_MEMORY_FILE, []),
            'profiles': self.plugin._load_json(USER_PROFILE_FILE, {}),
            'affection': self.plugin._affection,
            'personality': self.plugin._load_json(PERSONALITY_FILE, {}),
            'dynamic_log': self.plugin._load_json(DYNAMIC_LOG_FILE, []),
            'export_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
    
    def _get_html(self):
        return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BiliBot 管理面板</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --ice: #7ec8e3; --ice-dark: #4a9cc7; --bg: #0d1117; --card: rgba(22,27,34,0.8);
  --text: #e6edf3; --dim: #8b949e; --border: rgba(126,200,227,0.15);
}
body { font-family: -apple-system, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }
.login-overlay { position: fixed; inset: 0; background: var(--bg); display: flex; align-items: center; justify-content: center; z-index: 100; }
.login-box { background: var(--card); padding: 40px; border-radius: 16px; border: 1px solid var(--border); text-align: center; }
.login-box h2 { color: var(--ice); margin-bottom: 20px; }
.login-box input { width: 200px; padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: rgba(0,0,0,0.3); color: var(--text); margin-bottom: 15px; }
.login-box button { padding: 10px 30px; background: var(--ice); border: none; border-radius: 8px; color: #000; cursor: pointer; font-weight: 600; }
.app { display: none; }
.header { padding: 20px 30px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
.header h1 { color: var(--ice); font-size: 20px; }
.nav { display: flex; gap: 20px; padding: 15px 30px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.nav-btn { padding: 8px 16px; background: transparent; border: 1px solid var(--border); border-radius: 8px; color: var(--dim); cursor: pointer; }
.nav-btn.active, .nav-btn:hover { background: rgba(126,200,227,0.1); color: var(--ice); border-color: var(--ice); }
.panel { display: none; padding: 30px; }
.panel.active { display: block; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
.card h3 { color: var(--ice); margin-bottom: 15px; font-size: 16px; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }
.stat { text-align: center; padding: 15px; background: rgba(0,0,0,0.2); border-radius: 8px; }
.stat .num { font-size: 28px; color: var(--ice); font-weight: 700; }
.stat .label { font-size: 12px; color: var(--dim); margin-top: 5px; }
.list-item { padding: 12px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
.list-item:last-child { border: none; }
.list-item .text { flex: 1; font-size: 13px; color: var(--text); }
.list-item .meta { font-size: 11px; color: var(--dim); margin-left: 10px; }
.list-item .del { color: #e74c3c; cursor: pointer; font-size: 12px; margin-left: 10px; }
.empty { text-align: center; padding: 40px; color: var(--dim); }
.btn { padding: 8px 16px; background: var(--ice); border: none; border-radius: 6px; color: #000; cursor: pointer; font-size: 13px; }
.btn-sm { padding: 5px 10px; font-size: 11px; }
textarea { width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: rgba(0,0,0,0.3); color: var(--text); resize: vertical; min-height: 80px; margin-bottom: 10px; }
.tag { display: inline-block; padding: 3px 8px; background: rgba(126,200,227,0.15); border-radius: 4px; font-size: 11px; color: var(--ice); margin: 2px; }
</style>
</head>
<body>
<div class="login-overlay" id="loginOverlay">
  <div class="login-box">
    <h2>🌊 BiliBot</h2>
    <input type="password" id="pwdInput" placeholder="输入密码" onkeydown="if(event.key==='Enter')login()">
    <br><button onclick="login()">登录</button>
  </div>
</div>
<div class="app" id="app">
  <div class="header">
    <h1>🌊 BiliBot 管理面板</h1>
    <span id="statusDot" style="color:var(--dim)">检测中...</span>
  </div>
  <div class="nav">
    <button class="nav-btn active" onclick="showPanel('status')">📊 状态</button>
    <button class="nav-btn" onclick="showPanel('memory')">🧠 记忆</button>
    <button class="nav-btn" onclick="showPanel('affection')">💛 好感度</button>
    <button class="nav-btn" onclick="showPanel('permanent')">💎 永久记忆</button>
    <button class="nav-btn" onclick="showPanel('personality')">🌱 性格</button>
    <button class="nav-btn" onclick="showPanel('dynamic')">📝 动态</button>
    <button class="nav-btn" onclick="exportData()">📦 导出</button>
  </div>
  <div class="panel active" id="panel-status"></div>
  <div class="panel" id="panel-memory"></div>
  <div class="panel" id="panel-affection"></div>
  <div class="panel" id="panel-permanent"></div>
  <div class="panel" id="panel-personality"></div>
  <div class="panel" id="panel-dynamic"></div>
</div>
<script>
async function login() {
  const pwd = document.getElementById('pwdInput').value;
  const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:pwd})});
  const d = await r.json();
  if (d.ok) { document.getElementById('loginOverlay').style.display='none'; document.getElementById('app').style.display='block'; loadAll(); }
  else alert(d.error || '登录失败');
}
async function checkAuth() {
  const r = await fetch('/api/auth_check');
  const d = await r.json();
  if (d.ok) { document.getElementById('loginOverlay').style.display='none'; document.getElementById('app').style.display='block'; loadAll(); }
}
checkAuth();
function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  event.target.classList.add('active');
}
async function loadAll() { loadStatus(); loadMemory(); loadAffection(); loadPermanent(); loadPersonality(); loadDynamic(); }
async function loadStatus() {
  const r = await fetch('/api/status'); const d = await r.json();
  document.getElementById('statusDot').innerHTML = d.running ? '<span style="color:#2ecc71">🟢 运行中</span>' : '<span style="color:#e74c3c">🔴 未运行</span>';
  document.getElementById('panel-status').innerHTML = `
    <div class="card"><h3>📊 概览</h3><div class="stat-grid">
      <div class="stat"><div class="num">${d.memory_count}</div><div class="label">记忆条数</div></div>
      <div class="stat"><div class="num">${d.permanent_count}</div><div class="label">永久记忆</div></div>
      <div class="stat"><div class="num">${d.affection_count}</div><div class="label">好感度档案</div></div>
      <div class="stat"><div class="num">${d.profile_count}</div><div class="label">用户画像</div></div>
      <div class="stat"><div class="num">v${d.personality_version}</div><div class="label">性格版本</div></div>
      <div class="stat"><div class="num">${d.today_watched}</div><div class="label">今日视频</div></div>
      <div class="stat"><div class="num">${d.today_dynamic}</div><div class="label">今日动态</div></div>
    </div></div>
    <div class="card"><h3>⚙️ 功能开关</h3><div style="display:flex;flex-wrap:wrap;gap:10px;">
      ${Object.entries(d.features).map(([k,v])=>`<span class="tag">${k}: ${v?'✅':'❌'}</span>`).join('')}
    </div></div>`;
}
async function loadMemory(page=1) {
  const r = await fetch('/api/memory/list?page='+page); const d = await r.json();
  let html = '<div class="card"><h3>🧠 记忆列表 ('+d.total+'条)</h3>';
  if (d.items.length) {
    html += d.items.map(m => `<div class="list-item"><div class="text">${esc(m.text)}</div><span class="meta">${m.time} [${m.source}]</span><span class="del" onclick="delMemory('${m.id}')">删除</span></div>`).join('');
    html += `<div style="text-align:center;margin-top:15px;">`;
    if (page > 1) html += `<button class="btn btn-sm" onclick="loadMemory(${page-1})">上一页</button> `;
    html += `第${page}/${d.pages}页 `;
    if (page < d.pages) html += `<button class="btn btn-sm" onclick="loadMemory(${page+1})">下一页</button>`;
    html += `</div>`;
  } else html += '<div class="empty">暂无记忆</div>';
  html += '</div>';
  document.getElementById('panel-memory').innerHTML = html;
}
async function delMemory(rpid) { if(!confirm('确定删除?')) return; await fetch('/api/memory/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rpid})}); loadMemory(); }
async function loadAffection() {
  const r = await fetch('/api/affection/list'); const d = await r.json();
  let html = '<div class="card"><h3>💛 好感度排行 (共'+d.total+'人)</h3>';
  if (d.items.length) html += d.items.map((a,i) => `<div class="list-item"><span style="color:var(--ice);width:30px;">#${i+1}</span><span class="text">UID: ${a.uid}</span><span class="meta" style="font-size:16px;color:var(--ice);font-weight:bold;">${a.score}分</span></div>`).join('');
  else html += '<div class="empty">暂无数据</div>';
  html += '</div>';
  document.getElementById('panel-affection').innerHTML = html;
}
async function loadPermanent() {
  const r = await fetch('/api/permanent/list'); const d = await r.json();
  let html = '<div class="card"><h3>💎 永久记忆</h3><textarea id="newPerm" placeholder="添加新的永久记忆..."></textarea><button class="btn" onclick="addPerm()">添加</button><div style="margin-top:20px;">';
  if (d.items.length) html += d.items.map((p,i) => `<div class="list-item"><div class="text">${esc(p.text)}</div><span class="meta">${p.time||''}</span><span class="del" onclick="delPerm(${i})">删除</span></div>`).join('');
  else html += '<div class="empty">暂无永久记忆</div>';
  html += '</div></div>';
  document.getElementById('panel-permanent').innerHTML = html;
}
async function addPerm() { const t = document.getElementById('newPerm').value.trim(); if(!t) return; await fetch('/api/permanent/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})}); loadPermanent(); }
async function delPerm(i) { if(!confirm('确定删除?')) return; await fetch('/api/permanent/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:i})}); loadPermanent(); }
async function loadPersonality() {
  const r = await fetch('/api/personality'); const d = await r.json();
  document.getElementById('panel-personality').innerHTML = `
    <div class="card"><h3>🌱 性格演化 v${d.version}</h3><p style="color:var(--dim);font-size:13px;margin-bottom:15px;">上次演化: ${d.last_evolve}</p>
    <div style="margin-bottom:15px;"><strong>成长轨迹:</strong><div style="margin-top:8px;">${d.traits.length ? d.traits.map(t=>`<div style="padding:8px 0;border-bottom:1px solid var(--border);font-size:13px;">• ${esc(typeof t==='object'?(t.trait||'')+(t.trigger?' ('+t.trigger+')':''):t)}</div>`).join('') : '<span style="color:var(--dim)">暂无</span>'}</div></div>
    <div style="margin-bottom:15px;"><strong>语言习惯:</strong><div style="margin-top:8px;">${d.speech_habits.length ? d.speech_habits.map(h=>`<span class="tag">${esc(h)}</span>`).join('') : '<span style="color:var(--dim)">暂无</span>'}</div></div>
    <div style="margin-bottom:15px;"><strong>特殊看法:</strong><div style="margin-top:8px;">${d.opinions.length ? d.opinions.map(o=>`<span class="tag">${esc(o)}</span>`).join('') : '<span style="color:var(--dim)">暂无</span>'}</div></div>
    <div><strong>最近反思:</strong><p style="margin-top:8px;color:var(--dim);font-size:13px;">${esc(d.last_reflection) || '暂无'}</p></div>
    </div>`;
}
async function loadDynamic() {
  const r = await fetch('/api/dynamic/list'); const d = await r.json();
  let html = '<div class="card"><h3>📝 动态发布记录</h3>';
  if (d.items.length) html += d.items.slice().reverse().map(l => `<div class="list-item"><div class="text">${l.has_image?'🖼️':'📄'} ${esc(l.text)}</div><span class="meta">${l.time}</span></div>`).join('');
  else html += '<div class="empty">暂无动态记录</div>';
  html += '</div>';
  document.getElementById('panel-dynamic').innerHTML = html;
}
async function exportData() {
  const r = await fetch('/api/export'); const d = await r.json();
  const blob = new Blob([JSON.stringify(d,null,2)], {type:'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'bilibot_backup_'+new Date().toISOString().slice(0,10)+'.json'; a.click();
}
function esc(s) { return String(s||'').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script>
</body>
</html>'''
