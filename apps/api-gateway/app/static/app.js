/* =============================================================
 * 企业知识库问答 —— 前端逻辑
 * 设计约束：
 *   1. 零依赖、零构建、离线可用（不引 CDN）；
 *   2. 所有来自后端的文本先转义再渲染 Markdown，避免 XSS；
 *   3. 会话历史存 localStorage，后端不做多轮，前端只负责展示与回溯。
 * ============================================================= */
(function () {
  'use strict';

  var STORE_CONV = 'par.conversations.v2';
  var MAX_CONV = 50;
  var NEAR_BOTTOM_PX = 90;

  var SUGGESTIONS = [
    { q: '入职体检费用怎么报销？', tip: '福利 · 报销流程与上限' },
    { q: '年假有多少天？', tip: '考勤休假 · 按司龄分档' },
    { q: '入职体检报销需要在多久内提交？', tip: '查找具体时限' },
    { q: '公司年会在哪家酒店举办？', tip: '文档中不存在，应拒答' }
  ];

  // ---------------- 身份（OIDC 授权码 + PKCE） ----------------
  //
  // 走标准浏览器侧流程：不接触 client_secret，令牌只存在 sessionStorage。
  // 关闭鉴权（AUTHZ_ENABLED=false）时后端不校验令牌，这里静默降级为"未登录也可用"。

  var AUTH = {
    issuer: 'http://localhost:8180/realms/rag',
    clientId: 'rag-ui',
    redirectUri: window.location.origin + '/ui/',
    tokenKey: 'par.token',
    verifierKey: 'par.pkce_verifier',
    stateKey: 'par.oauth_state'
  };

  function b64url(bytes) {
    var binary = '';
    new Uint8Array(bytes).forEach(function (b) { binary += String.fromCharCode(b); });
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function randomString(length) {
    var bytes = new Uint8Array(length);
    crypto.getRandomValues(bytes);
    return b64url(bytes).slice(0, length);
  }

  function storedToken() {
    try {
      var raw = sessionStorage.getItem(AUTH.tokenKey);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (parsed.expires_at && Date.now() > parsed.expires_at) {
        sessionStorage.removeItem(AUTH.tokenKey);
        return null;
      }
      return parsed.access_token || null;
    } catch (err) {
      return null;
    }
  }

  function storeToken(payload) {
    payload.expires_at = Date.now() + Math.max(0, (payload.expires_in || 300) - 30) * 1000;
    try {
      sessionStorage.setItem(AUTH.tokenKey, JSON.stringify(payload));
    } catch (err) {
      console.error('[auth] 无法保存令牌', err);
    }
  }

  function clearToken() {
    try { sessionStorage.removeItem(AUTH.tokenKey); } catch (err) { /* 忽略 */ }
  }

  function claimsOf(accessToken) {
    try {
      var part = accessToken.split('.')[1];
      part += '='.repeat((-part.length % 4 + 4) % 4);
      return JSON.parse(atob(part.replace(/-/g, '+').replace(/_/g, '/')));
    } catch (err) {
      return {};
    }
  }

  function signIn() {
    crypto.subtle.digest('SHA-256', new TextEncoder().encode(randomString(64)))
      .then(function (digest) {
        var verifier = randomString(64);
        return crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)).then(function (d) {
          try {
            sessionStorage.setItem(AUTH.verifierKey, verifier);
            sessionStorage.setItem(AUTH.stateKey, randomString(16));
          } catch (err) { /* 忽略 */ }
          var params = new URLSearchParams({
            response_type: 'code',
            client_id: AUTH.clientId,
            redirect_uri: AUTH.redirectUri,
            scope: 'openid profile',
            code_challenge: b64url(d),
            code_challenge_method: 'S256',
            state: sessionStorage.getItem(AUTH.stateKey) || ''
          });
          void digest;
          window.location.href = AUTH.issuer + '/protocol/openid-connect/auth?' + params.toString();
        });
      })
      .catch(function (err) {
        toast('浏览器不支持 PKCE，无法登录：' + err.message, 'fail');
      });
  }

  function signOut() {
    clearToken();
    renderAuthState();
    toast('已退出登录', 'ok');
    refreshKbCount();
  }

  function completeSignIn() {
    var params = new URLSearchParams(window.location.search);
    var code = params.get('code');
    if (!code) return Promise.resolve();
    var state = params.get('state');
    var expected = sessionStorage.getItem(AUTH.stateKey);
    if (expected && state !== expected) {
      toast('登录回调 state 不匹配，已忽略', 'fail');
      window.history.replaceState({}, '', AUTH.redirectUri);
      return Promise.resolve();
    }
    var verifier = sessionStorage.getItem(AUTH.verifierKey) || '';
    var body = new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: AUTH.clientId,
      code: code,
      redirect_uri: AUTH.redirectUri,
      code_verifier: verifier
    });
    return fetch(AUTH.issuer + '/protocol/openid-connect/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString()
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) {
            // 令牌交换失败要能在控制台看到原始响应，否则只能靠猜
            console.error('[auth] token exchange failed', resp.status, data);
            throw new Error(data.error_description || data.error || ('HTTP ' + resp.status));
          }
          storeToken(data);
        });
      })
      .catch(function (err) {
        console.error('[auth] sign-in failed', err);
        toast('登录失败：' + ((err && err.message) || err), 'fail');
      })
      .then(function () {
        window.history.replaceState({}, '', AUTH.redirectUri);
      });
  }

  function authFetch(url, options) {
    var opts = options || {};
    var token = storedToken();
    if (token) {
      opts.headers = Object.assign({}, opts.headers, { Authorization: 'Bearer ' + token });
    }
    return fetch(url, opts).then(function (resp) {
      // 只在「这个失败请求用的就是当前令牌」时清空。
      // 否则会出现：登录回调刚把新令牌写进去，而早于登录发出的请求（如 /admin/models）
      // 的 401 姗姗来迟，把刚到手的令牌又清掉——表现为"登录成功却立刻变成未登录"。
      if (resp.status === 401 && token && storedToken() === token) {
        clearToken();
        renderAuthState();
      }
      return resp;
    });
  }

  function renderAuthState() {
    var token = storedToken();
    var label = $('authLabel');
    var button = $('authBtn');
    if (!token) {
      label.textContent = '未登录';
      button.textContent = '登录';
      button.dataset.action = 'login';
      return;
    }
    var claims = claimsOf(token);
    var name = claims.preferred_username || claims.name || claims.sub || '已登录';
    label.textContent = name + ' · ' + (claims.department_id || '-');
    button.textContent = '退出';
    button.dataset.action = 'logout';
  }

  // ---------------- 工具 ----------------

  function $(id) { return document.getElementById(id); }

  function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function fmtSeconds(ms) {
    if (ms == null) return null;
    return (ms / 1000).toFixed(1) + 's';
  }

  function truncate(text, n) {
    var t = String(text || '').replace(/\s+/g, ' ').trim();
    return t.length > n ? t.slice(0, n) + '…' : t;
  }

  function uid() {
    return 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  }

  function greeting() {
    var h = new Date().getHours();
    if (h < 6) return '夜深了';
    if (h < 12) return '早上好';
    if (h < 14) return '中午好';
    if (h < 18) return '下午好';
    return '晚上好';
  }

  // ---------------- Markdown（最小子集，安全优先） ----------------

  function renderCitationChip(n, citations) {
    var known = (citations || []).some(function (c) { return Number(c.index) === Number(n); });
    if (!known) return '<sup class="cite cite-missing" title="该编号没有对应来源">' + n + '</sup>';
    return '<sup class="cite" data-cite="' + n + '" role="button" tabindex="0" title="查看来源 ' + n + '">' + n + '</sup>';
  }

  /**
   * 极简 Markdown 渲染：代码块 / 标题 / 列表 / 引用 / 行内代码 / 粗斜体 / 链接 / 引用角标。
   * 先整体转义再套标签，避免任何 HTML 注入。
   */
  function renderMarkdown(source, citations) {
    var text = escapeHtml(String(source || '').trim());
    if (!text) return '';

    // 1) 代码块先摘出来占位，避免内部内容被后续规则改写
    var codeBlocks = [];
    text = text.replace(/```([\s\S]*?)```/g, function (_, code) {
      codeBlocks.push(code);
      return '\u0000CODE' + (codeBlocks.length - 1) + '\u0000';
    });

    // 2) 块级元素
    text = text.replace(/^(#{1,6})[ \t]+(.*)$/gm, function (_, hashes, title) {
      var level = Math.min(hashes.length + 2, 6); // 页面已有 h1/h2，正文从 h3 起
      return '<h' + level + '>' + title + '</h' + level + '>';
    });
    text = text.replace(/^(?:&gt;[ \t]?.*(?:\n|$))+/gm, function (block) {
      return '<blockquote>' + block.replace(/^&gt;[ \t]?/gm, '').trim() + '</blockquote>';
    });
    text = text.replace(/^(?:[ \t]*[-*][ \t]+.*(?:\n|$))+/gm, function (block) {
      var items = block.trim().split('\n').map(function (line) {
        return '<li>' + line.replace(/^[ \t]*[-*][ \t]+/, '') + '</li>';
      });
      return '<ul>' + items.join('') + '</ul>';
    });
    text = text.replace(/^(?:[ \t]*\d+\.[ \t]+.*(?:\n|$))+/gm, function (block) {
      var items = block.trim().split('\n').map(function (line) {
        return '<li>' + line.replace(/^[ \t]*\d+\.[ \t]+/, '') + '</li>';
      });
      return '<ol>' + items.join('') + '</ol>';
    });

    // 3) 行内元素（链接必须先于引用角标处理）
    text = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    text = text.replace(/\[(\d+)\]/g, function (whole, n) {
      return renderCitationChip(n, citations);
    });

    // 4) 段落
    text = text.split(/\n{2,}/).map(function (block) {
      if (/^<(h\d|ul|ol|blockquote|pre)/.test(block.trim())) return block;
      return '<p>' + block.replace(/\n/g, '<br>') + '</p>';
    }).join('');

    // 5) 还原代码块
    text = text.replace(/\u0000CODE(\d+)\u0000/g, function (_, i) {
      return '<pre><code>' + codeBlocks[Number(i)].replace(/^\n+|\n+$/g, '') + '</code></pre>';
    });
    return text;
  }

  // ---------------- 状态 ----------------

  var conversations = [];
  var currentId = null;
  var busy = false;
  var abortController = null;

  function loadConversations() {
    try {
      var raw = localStorage.getItem(STORE_CONV);
      conversations = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(conversations)) conversations = [];
    } catch (err) {
      conversations = [];
    }
  }

  function saveConversations() {
    try {
      localStorage.setItem(STORE_CONV, JSON.stringify(conversations.slice(0, MAX_CONV)));
    } catch (err) {
      /* 隐私模式或超额：忽略，不阻断对话 */
    }
  }

  function currentConv() {
    var conv = conversations.filter(function (c) { return c.id === currentId; })[0];
    if (!conv) {
      conv = { id: uid(), title: '新对话', createdAt: Date.now(), messages: [] };
      conversations.unshift(conv);
      currentId = conv.id;
    }
    return conv;
  }

  function newConversation() {
    var conv = { id: uid(), title: '新对话', createdAt: Date.now(), messages: [] };
    conversations.unshift(conv);
    currentId = conv.id;
    saveConversations();
    renderSidebar();
    renderMessages();
    closeSidebarOnMobile();
    $('input').focus();
  }

  // ---------------- 渲染：侧栏 ----------------

  function renderSidebar() {
    var list = $('convList');
    list.innerHTML = '';
    $('convEmpty').hidden = conversations.length > 0;

    conversations.forEach(function (conv) {
      var item = document.createElement('a');
      item.className = 'conv-item' + (conv.id === currentId ? ' active' : '');
      item.href = '#';
      item.innerHTML = '<span class="conv-name"></span>' +
        '<button class="conv-del" type="button" title="删除对话" aria-label="删除对话">✕</button>';
      item.querySelector('.conv-name').textContent = conv.title || '新对话';

      item.addEventListener('click', function (event) {
        if (event.target.closest('.conv-del')) return;
        event.preventDefault();
        currentId = conv.id;
        renderSidebar();
        renderMessages();
        closeSidebarOnMobile();
      });

      item.querySelector('.conv-del').addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        conversations = conversations.filter(function (c) { return c.id !== conv.id; });
        if (currentId === conv.id) currentId = conversations.length ? conversations[0].id : null;
        saveConversations();
        renderSidebar();
        renderMessages();
      });

      list.appendChild(item);
    });
  }

  // ---------------- 渲染：消息 ----------------

  function buildActions(message) {
    var wrap = document.createElement('div');
    wrap.className = 'actions';

    var copyBtn = document.createElement('button');
    copyBtn.className = 'act';
    copyBtn.type = 'button';
    copyBtn.textContent = '复制';
    copyBtn.addEventListener('click', function () {
      copyText(message.content).then(function (ok) {
        copyBtn.textContent = ok ? '已复制' : '复制失败';
        copyBtn.classList.toggle('done', ok);
        setTimeout(function () { copyBtn.textContent = '复制'; copyBtn.classList.remove('done'); }, 1600);
      });
    });
    wrap.appendChild(copyBtn);

    var regenBtn = document.createElement('button');
    regenBtn.className = 'act';
    regenBtn.type = 'button';
    regenBtn.textContent = '重新生成';
    regenBtn.disabled = busy;
    regenBtn.addEventListener('click', function () {
      var lastUser = lastUserQuery();
      if (lastUser) send(lastUser, { skipAppendUser: true });
    });
    wrap.appendChild(regenBtn);

    return wrap;
  }

  function buildSources(citations) {
    var wrap = document.createElement('div');
    wrap.className = 'sources';

    var head = document.createElement('div');
    head.className = 'sources-head';
    head.innerHTML = '<span>引用来源 · ' + citations.length + ' 条</span>';
    var toggle = document.createElement('button');
    toggle.className = 'toggle';
    toggle.type = 'button';
    toggle.textContent = '收起';
    head.appendChild(toggle);
    wrap.appendChild(head);

    var body = document.createElement('div');
    body.className = 'sources-body';

    citations.forEach(function (c) {
      var card = document.createElement('div');
      card.className = 'source';
      card.dataset.cite = String(c.index);

      var score = (c.score == null) ? '' : ' · 相关度 ' + Number(c.score).toFixed(4);
      card.innerHTML =
        '<div class="source-head">' +
          '<span class="source-badge">[' + escapeHtml(c.index) + ']</span>' +
          '<span class="source-doc"></span>' +
          '<span class="source-path"></span>' +
        '</div>' +
        '<div class="source-snippet"></div>' +
        '<div class="source-loc">分块 #' + escapeHtml(c.chunk_index) +
          ' · 字符 ' + escapeHtml(c.char_start) + '–' + escapeHtml(c.char_end) +
          escapeHtml(score) + '</div>';

      card.querySelector('.source-doc').textContent = c.doc_title || c.doc_id || '未命名文档';
      card.querySelector('.source-path').textContent = c.section_path || '';
      card.querySelector('.source-snippet').textContent = c.snippet || '';
      card.addEventListener('click', function () { card.classList.toggle('open'); });
      body.appendChild(card);
    });

    toggle.addEventListener('click', function () {
      var collapsed = body.hidden;
      body.hidden = !collapsed;
      toggle.textContent = collapsed ? '收起' : '展开';
    });

    wrap.appendChild(body);
    return wrap;
  }

  function buildMeta(message) {
    var t = message.timings || {};
    var parts = [];
    if (message.cached) parts.push('命中缓存');
    if (t.retrieve != null) parts.push('检索 ' + fmtSeconds(t.retrieve));
    if (t.generate != null) parts.push('生成 ' + fmtSeconds(t.generate));
    if (t.total != null) parts.push('总 ' + fmtSeconds(t.total));
    if (message.model) parts.push(message.model);
    if (!parts.length) return null;
    var el = document.createElement('div');
    el.className = 'meta';
    el.textContent = parts.join(' · ');
    return el;
  }

  function buildAssistantMessage(message) {
    var row = document.createElement('div');
    row.className = 'msg assistant';

    var avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = 'AI';
    row.appendChild(avatar);

    var body = document.createElement('div');
    body.className = 'body';

    if (message.error) {
      var err = document.createElement('div');
      err.className = 'notice error';
      err.innerHTML = '<strong>请求失败</strong>';
      err.appendChild(document.createTextNode(message.error));
      body.appendChild(err);
    } else if (message.refused) {
      // 拒答不是错误，是正确行为：用中性提示，且不展示任何引用
      var notice = document.createElement('div');
      notice.className = 'notice';
      notice.innerHTML = '<strong>未在当前知识库中找到相关资料</strong>';
      notice.appendChild(document.createTextNode('已按「无资料即拒答」策略返回，未生成任何推测性内容。'));
      body.appendChild(notice);
    } else {
      var content = document.createElement('div');
      content.className = 'content';
      content.innerHTML = renderMarkdown(message.content, message.citations);
      body.appendChild(content);
    }

    if (message.citations && message.citations.length) {
      body.appendChild(buildSources(message.citations));
    }

    body.appendChild(buildActions(message));
    var meta = buildMeta(message);
    if (meta) body.appendChild(meta);

    row.appendChild(body);
    return row;
  }

  function buildUserMessage(message) {
    var row = document.createElement('div');
    row.className = 'msg user';
    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = message.content;
    row.appendChild(bubble);
    return row;
  }

  function buildWelcome() {
    var section = document.createElement('section');
    section.className = 'welcome';
    section.innerHTML = '<h1></h1><p>基于企业内部资料的问答，答案附带可回查的原文引用。</p>';
    section.querySelector('h1').textContent = greeting() + '，我是 permission-aware-rag';

    var grid = document.createElement('div');
    grid.className = 'suggestions';
    SUGGESTIONS.forEach(function (item) {
      var btn = document.createElement('button');
      btn.className = 'suggestion';
      btn.type = 'button';
      btn.innerHTML = '<svg class="sug-ico" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">' +
        '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H10l2 2h6.5A1.5 1.5 0 0 1 20 7.5v9A1.5 1.5 0 0 1 18.5 18h-13A1.5 1.5 0 0 1 4 16.5v-11Z" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>' +
        '<span class="sug-text"><span class="sug-q"></span><span class="sug-tip"></span></span>';
      btn.querySelector('.sug-q').textContent = item.q;
      btn.querySelector('.sug-tip').textContent = item.tip;
      btn.addEventListener('click', function () { send(item.q); });
      grid.appendChild(btn);
    });
    section.appendChild(grid);
    return section;
  }

  function renderMessages() {
    var container = $('messages');
    var conv = currentConv();
    container.innerHTML = '';

    if (!conv.messages.length) {
      container.appendChild(buildWelcome());
    } else {
      conv.messages.forEach(function (message) {
        container.appendChild(
          message.role === 'user' ? buildUserMessage(message) : buildAssistantMessage(message)
        );
      });
    }

    $('topTitle').textContent = conv.title || '新对话';
    scrollToBottom(true);
  }

  // ---------------- 思考中占位 ----------------

  function showThinking() {
    var container = $('messages');
    var welcome = container.querySelector('.welcome');
    if (welcome) welcome.remove();

    var row = document.createElement('div');
    row.className = 'msg assistant';
    row.innerHTML =
      '<div class="avatar">AI</div>' +
      '<div class="body"><div class="thinking">' +
        '<span class="dots"><i></i><i></i><i></i></span>' +
        '<span>正在检索资料并生成回答</span>' +
        '<span class="elapsed">0.0s</span>' +
      '</div></div>';
    container.appendChild(row);
    scrollToBottom(true);

    var elapsedEl = row.querySelector('.elapsed');
    var started = Date.now();
    var timer = setInterval(function () {
      elapsedEl.textContent = fmtSeconds(Date.now() - started) || '0.0s';
    }, 120);

    $('statusHint').textContent = '检索中…';
    return {
      remove: function () {
        clearInterval(timer);
        row.remove();
        $('statusHint').textContent = '';
      }
    };
  }

  // ---------------- 滚动 ----------------

  function nearBottom() {
    var el = $('messages');
    return el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_PX;
  }

  function scrollToBottom(force) {
    var el = $('messages');
    if (force || nearBottom()) el.scrollTop = el.scrollHeight;
  }

  // ---------------- 发送 ----------------

  function lastUserQuery() {
    var conv = currentConv();
    for (var i = conv.messages.length - 1; i >= 0; i -= 1) {
      if (conv.messages[i].role === 'user') return conv.messages[i].content;
    }
    return '';
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return false; });
    }
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return Promise.resolve(ok);
    } catch (err) {
      return Promise.resolve(false);
    }
  }

  function setBusy(value) {
    busy = value;
    var btn = $('send');
    btn.classList.toggle('busy', value);
    btn.setAttribute('aria-label', value ? '停止' : '发送');
    $('input').disabled = value;
  }

  function buildChatPayload(query) {
    var payload = { query: query };

    // 选了具体模型才下发；留空表示"由网关挑默认模型"。
    // mode / top_k / temperature 一律不传：由后端按 query 自行决定检索策略。
    var model = $('modelSelect').value;
    if (model) payload.model = model;

    return payload;
  }

  function send(rawQuery, options) {
    var query = String(rawQuery || '').replace(/\s+$/, '');
    if (!query || busy) return;

    var opts = options || {};
    var conv = currentConv();

    if (!opts.skipAppendUser) {
      conv.messages.push({ role: 'user', content: query });
    }
    if (conv.title === '新对话' || !conv.title) {
      conv.title = truncate(query, 18);
    }
    saveConversations();
    renderSidebar();
    renderMessages();

    $('input').value = '';
    autoGrow();
    setBusy(true);
    var thinking = showThinking();

    abortController = new AbortController();

    authFetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildChatPayload(query)),
      signal: abortController.signal
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) {
            throw new Error(data.message || data.detail || ('HTTP ' + resp.status));
          }
          return data;
        });
      })
      .then(function (data) {
        conv.messages.push({
          role: 'assistant',
          content: data.answer || '',
          citations: data.citations || [],
          refused: !!data.refused,
          cached: !!data.cached,
          model: data.model || null,
          timings: data.timings_ms || {}
        });
        if (data.model) setActiveModel(data.model);
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') {
          conv.messages.push({
            role: 'assistant',
            content: '',
            error: '已停止等待（服务端可能仍在处理该请求）'
          });
        } else {
          conv.messages.push({
            role: 'assistant',
            content: '',
            error: String((err && err.message) || err)
          });
        }
      })
      .then(function () {
        thinking.remove();
        setBusy(false);
        abortController = null;
        if (conv.messages.length === 0) return;
        saveConversations();
        renderSidebar();
        renderMessages();
        $('input').focus();
        var last = conv.messages[conv.messages.length - 1];
        if (last && last.timings && last.timings.total != null) {
          $('statusHint').textContent = '本次耗时 ' + fmtSeconds(last.timings.total);
        }
      });
  }

  // ---------------- 输入框 ----------------

  function autoGrow() {
    var input = $('input');
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 190) + 'px';
  }

  function wireInput() {
    var input = $('input');
    input.addEventListener('input', autoGrow);
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        send(input.value);
      }
    });
    $('send').addEventListener('click', function () {
      if (busy && abortController) {
        abortController.abort();
        return;
      }
      send(input.value);
    });
    // 药丸内左侧「+」：复用上传弹窗
    $('composerAttach').addEventListener('click', function () {
      openModal('uploadModal');
    });
  }

  // ---------------- 引用跳转 ----------------

  function wireCitations() {
    $('messages').addEventListener('click', function (event) {
      var chip = event.target.closest('.cite[data-cite]');
      if (!chip) return;
      var row = chip.closest('.msg');
      var card = row && row.querySelector('.source[data-cite="' + chip.dataset.cite + '"]');
      if (!card) return;
      card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      card.classList.add('flash');
      setTimeout(function () { card.classList.remove('flash'); }, 1400);
    });
  }

  // ---------------- 主题与侧栏 ----------------

  function wireCollapse() {
    var app = document.querySelector('.app');
    var btn = $('sidebarToggle');
    var expand = $('sidebarExpand');
    var KEY = 'par.sidebar';
    try {
      if (localStorage.getItem(KEY) === 'collapsed') app.classList.add('collapsed');
    } catch (err) { /* 忽略 */ }
    function toggle() {
      var collapsed = app.classList.toggle('collapsed');
      try { localStorage.setItem(KEY, collapsed ? 'collapsed' : 'open'); } catch (err) { /* 忽略 */ }
    }
    if (btn) btn.addEventListener('click', toggle);
    if (expand) expand.addEventListener('click', toggle);
  }

  function openSidebar() {
    $('sidebar').classList.add('open');
    $('scrim').classList.add('show');
  }

  function closeSidebarOnMobile() {
    if (window.innerWidth <= 900) {
      $('sidebar').classList.remove('open');
      $('scrim').classList.remove('show');
    }
  }

  function wireSidebar() {
    $('newChat').addEventListener('click', newConversation);
    $('sidebarOpen').addEventListener('click', openSidebar);
    $('sidebarClose').addEventListener('click', closeSidebarOnMobile);
    $('scrim').addEventListener('click', closeSidebarOnMobile);
  }

  function wireScroll() {
    $('messages').addEventListener('scroll', function () {
      $('scrollBottom').classList.toggle('show', !nearBottom());
    });
    $('scrollBottom').addEventListener('click', function () { scrollToBottom(true); });
  }

  // ---------------- 轻提示 ----------------

  function toast(message, kind) {
    var wrap = $('toasts');
    var el = document.createElement('div');
    el.className = 'toast ' + (kind || '');
    el.textContent = message;
    wrap.appendChild(el);
    setTimeout(function () {
      el.style.transition = 'opacity .2s';
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 220);
    }, 3200);
  }

  // ---------------- 弹窗 ----------------

  function openModal(id) {
    var modal = $(id);
    modal.hidden = false;
    var focusable = modal.querySelector('input, button.btn, button.btn-ghost');
    if (focusable) focusable.focus();
  }

  function closeModal(modal) {
    modal.hidden = true;
  }

  function wireModals() {
    document.addEventListener('click', function (event) {
      var closer = event.target.closest('[data-close]');
      if (closer) {
        var modal = closer.closest('.modal');
        if (modal) closeModal(modal);
      }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      var open = Array.prototype.filter.call(
        document.querySelectorAll('.modal'), function (m) { return !m.hidden; }
      );
      if (open.length) closeModal(open[open.length - 1]);
    });
  }

  // ---------------- 上传 / 知识库 ----------------

  function fmtBytes(bytes) {
    if (!bytes && bytes !== 0) return '';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }

  function fmtDate(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso);
      return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    } catch (err) {
      return '';
    }
  }

  var STATUS_TEXT = { indexed: '已索引', pending: '待索引', running: '进行中', failed: '失败' };

  function options() {
    return {
      visibility: $('visibility').value,
      reindex: $('reindex').checked
    };
  }

  function makeQueueItem(name) {
    var el = document.createElement('div');
    el.className = 'queue-item';
    el.innerHTML = '<span class="queue-name"></span><span class="badge">排队中</span>' +
      '<span class="queue-detail"></span>';
    el.querySelector('.queue-name').textContent = name;
    var badge = el.querySelector('.badge');
    var detail = el.querySelector('.queue-detail');
    return {
      el: el,
      badge: function (text, cls) { badge.textContent = text; badge.className = 'badge ' + (cls || ''); },
      detail: function (text, isError) { detail.textContent = text; detail.className = 'queue-detail' + (isError ? ' error' : ''); }
    };
  }

  function uploadOne(file, item, opts) {
    var form = new FormData();
    form.append('file', file, file.name);
    form.append('visibility', opts.visibility);
    form.append('reindex', opts.reindex ? 'true' : 'false');
    return authFetch('/documents/upload', { method: 'POST', body: form }).then(function (resp) {
      return resp.json().then(function (data) {
        if (!resp.ok) throw new Error(data.message || data.detail || ('HTTP ' + resp.status));
        return data;
      });
    });
  }

  function startUploads(files) {
    if (!files || !files.length) return;
    var opts = options();
    var list = $('queue');
    list.hidden = false;

    var pending = Array.prototype.slice.call(files);
    var done = 0;
    var failed = 0;
    $('uploadSummary').textContent = '共 ' + pending.length + ' 个文件，开始上传…';

    // 串行上传：入库本身会打满 Milvus/OpenSearch 的写入路径，并发只会互相拖慢
    var chain = Promise.resolve();
    pending.forEach(function (file) {
      var item = makeQueueItem(file.name);
      list.appendChild(item.el);
      chain = chain.then(function () {
        item.badge('上传中', 'running');
        return uploadOne(file, item, opts).then(function (data) {
          done += 1;
          item.badge('完成', 'ok');
          item.detail('分块 ' + data.chunk_count + ' 个 · doc_id ' + (data.doc_ids || []).join(','));
        }).catch(function (err) {
          failed += 1;
          item.badge('失败', 'fail');
          item.detail(String((err && err.message) || err), true);
        });
      });
    });

    chain.then(function () {
      $('uploadSummary').textContent = '成功 ' + done + ' 个' + (failed ? '，失败 ' + failed + ' 个' : '');
      toast('上传完成：成功 ' + done + ' 个' + (failed ? '，失败 ' + failed + ' 个' : ''), failed ? 'fail' : 'ok');
      refreshKbCount();
      if (!$('kbModal').hidden) loadKbList();
    });
  }

  function importServerPath() {
    var path = $('serverPath').value.trim();
    if (!path) {
      toast('请先填写服务器路径', 'fail');
      return;
    }
    var opts = options();
    var btn = $('pathImport');
    btn.disabled = true;
    btn.textContent = '导入中…';
    $('uploadSummary').textContent = '正在解析并入索引，目录较大时需要等待…';

    authFetch('/documents/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: path,
        reindex: opts.reindex,
        acl: { visibility: opts.visibility }
      })
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) throw new Error(data.message || data.detail || ('HTTP ' + resp.status));
          return data;
        });
      })
      .then(function (data) {
        $('uploadSummary').textContent =
          '导入完成：文档 ' + data.documents + ' 篇，分块 ' + data.chunk_count + ' 个，耗时 ' +
          (data.timings_ms && data.timings_ms.total ? (data.timings_ms.total / 1000).toFixed(1) + 's' : '-');
        toast('已导入 ' + data.documents + ' 篇文档（' + data.chunk_count + ' 个分块）', 'ok');
        refreshKbCount();
        if (!$('kbModal').hidden) loadKbList();
      })
      .catch(function (err) {
        $('uploadSummary').textContent = '';
        toast('导入失败：' + ((err && err.message) || err), 'fail');
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = '导入';
      });
  }

  function loadKbList() {
    var list = $('kbList');
    var keyword = $('kbSearch').value.trim();
    list.innerHTML = '<div class="kb-empty">加载中…</div>';

    return authFetch('/documents?limit=100' + (keyword ? '&keyword=' + encodeURIComponent(keyword) : ''))
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) throw new Error(data.message || data.detail || ('HTTP ' + resp.status));
          return data;
        });
      })
      .then(function (data) {
        var items = data.items || [];
        $('kbFooter').textContent = '共 ' + (data.total || 0) + ' 篇文档';
        list.innerHTML = '';
        if (!items.length) {
          list.innerHTML = '<div class="kb-empty">' +
            (keyword ? '没有匹配「' + escapeHtml(keyword) + '」的文档' : '知识库还是空的<br>点上方「上传」把资料加进来') +
            '</div>';
          return;
        }
        items.forEach(function (doc) { list.appendChild(buildKbItem(doc)); });
      })
      .catch(function (err) {
        list.innerHTML = '<div class="kb-empty">加载失败：' + escapeHtml(String((err && err.message) || err)) + '</div>';
      });
  }

  function buildKbItem(doc) {
    var el = document.createElement('div');
    el.className = 'kb-item';
    el.innerHTML =
      '<div class="kb-title"></div>' +
      '<div class="kb-meta">' +
        '<span class="kb-stats"></span>' +
        '<div class="kb-ops">' +
          '<button class="op" type="button" data-op="reindex">重建索引</button>' +
          '<button class="op danger" type="button" data-op="delete">删除</button>' +
        '</div>' +
      '</div>' +
      '<div class="kb-source"></div>';

    el.querySelector('.kb-title').textContent = doc.title || doc.doc_id;
    el.querySelector('.kb-source').textContent = doc.source || '';
    el.querySelector('.kb-stats').textContent =
      (STATUS_TEXT[doc.status] || doc.status) + ' · ' + (doc.chunk_count || 0) + ' 分块 · ' +
      fmtBytes(doc.size_bytes) + ' · ' + fmtDate(doc.updated_at);

    el.querySelector('[data-op="reindex"]').addEventListener('click', function (event) {
      reindexDoc(doc, event.currentTarget);
    });
    el.querySelector('[data-op="delete"]').addEventListener('click', function () {
      deleteDoc(doc);
    });
    return el;
  }

  function reindexDoc(doc, btn) {
    if (!doc.source) {
      toast('该文档没有来源路径，无法重建索引', 'fail');
      return;
    }
    btn.disabled = true;
    btn.textContent = '重建中…';
    authFetch('/documents/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: doc.source, reindex: true })
    })
      .then(function (resp) {
        return resp.json().then(function (data) {
          if (!resp.ok) throw new Error(data.message || data.detail || ('HTTP ' + resp.status));
          return data;
        });
      })
      .then(function (data) {
        toast('已重建：' + doc.title + '（' + data.chunk_count + ' 个分块）', 'ok');
        loadKbList();
      })
      .catch(function (err) {
        toast('重建失败：' + ((err && err.message) || err), 'fail');
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = '重建索引';
      });
  }

  function deleteDoc(doc) {
    if (!window.confirm('确定从知识库删除「' + (doc.title || doc.doc_id) + '」？\n将同时清除向量索引、全文索引与元数据。')) return;
    authFetch('/documents/' + encodeURIComponent(doc.doc_id), { method: 'DELETE' })
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (data) {
            throw new Error(data.message || data.detail || ('HTTP ' + resp.status));
          });
        }
        return resp.json();
      })
      .then(function () {
        toast('已删除「' + (doc.title || doc.doc_id) + '」', 'ok');
        refreshKbCount();
        loadKbList();
      })
      .catch(function (err) {
        toast('删除失败：' + ((err && err.message) || err), 'fail');
      });
  }

  function refreshKbCount() {
    return authFetch('/documents?limit=1')
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        var badge = $('kbCount');
        if (!data) { badge.hidden = true; return; }
        var total = data.total || 0;
        badge.textContent = String(total);
        badge.hidden = total === 0;
      })
      .catch(function () { /* 忽略：计数失败不该干扰问答 */ });
  }

  function loadKbListIfOpen() {
    if (!$('kbModal').hidden) loadKbList();
  }

  function wireAuth() {
    $('authBtn').addEventListener('click', function () {
      if ($('authBtn').dataset.action === 'logout') signOut();
      else signIn();
    });
  }

  function wireKnowledge() {
    $('uploadOpen').addEventListener('click', function () { openModal('uploadModal'); });
    $('kbUpload').addEventListener('click', function () { openModal('uploadModal'); });
    $('kbOpen').addEventListener('click', function () {
      openModal('kbModal');
      loadKbList();
    });
    $('kbRefresh').addEventListener('click', loadKbList);
    $('pathImport').addEventListener('click', importServerPath);

    var debounce = null;
    $('kbSearch').addEventListener('input', function () {
      clearTimeout(debounce);
      debounce = setTimeout(loadKbList, 300);
    });

    // 选择文件
    var picker = $('filePicker');
    $('dropzone').addEventListener('click', function () { picker.click(); });
    $('dropzone').addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        picker.click();
      }
    });
    picker.addEventListener('change', function () {
      startUploads(picker.files);
      picker.value = '';
    });

    // 拖拽上传
    var zone = $('dropzone');
    ['dragenter', 'dragover'].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.add('dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.remove('dragover');
      });
    });
    zone.addEventListener('drop', function (event) {
      var files = (event.dataTransfer && event.dataTransfer.files) || [];
      startUploads(files);
    });

    // 整页拖入也能上传：避免用户必须先打开弹窗
    window.addEventListener('dragover', function (event) { event.preventDefault(); });
    window.addEventListener('drop', function (event) {
      if (event.target.closest && event.target.closest('#dropzone')) return;
      event.preventDefault();
      var files = (event.dataTransfer && event.dataTransfer.files) || [];
      if (!files.length) return;
      openModal('uploadModal');
      startUploads(files);
    });
  }

  // ---------------- 启动 ----------------

  function init() {
    loadConversations();
    if (conversations.length) currentId = conversations[0].id;
    else newConversation();

    wireCollapse();
    wireSidebar();
    wireInput();
    wireCitations();
    wireScroll();
    wireModals();
    wireKnowledge();
    wireAuth();
    renderSidebar();
    renderMessages();
    $('input').focus();

    // 先消费 OAuth 回调（如果有），再渲染登录态与知识库计数
    completeSignIn().then(function () {
      renderAuthState();
      refreshKbCount();
      if (storedToken()) loadKbListIfOpen();
    });

    // 顶栏模型下拉（失败时静默降级成"默认"，不影响问答）
    loadModels();
  }

  // 把服务端回报的"实际生效模型"同步回下拉。
  // 注意这**不是**回显用户的选择：请求可能被缓存命中或模型不可用而落到别的模型，
  // 只有服务端知道真正作答的是哪个。
  function setActiveModel(name) {
    var select = $('modelSelect');
    if (!select || !name) return;
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === name) {
        select.value = name;
        return;
      }
    }
    // 服务端用了清单里没有的模型（例如兜底链切到了备用模型）：补进去而不是丢弃，
    // 否则下拉会停留在一个与实际不符的选项上。
    var opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    select.appendChild(opt);
    select.value = name;
  }

  function loadModels() {
    authFetch('/chat/models')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (models) {
        if (!models || !models.length) return;
        // 只列对话模型：把 embedding 模型放进下拉是误导，选它做不了任何事
        var chatModels = models.filter(function (m) {
          return m.kind === 'chat' && m.available !== false;
        });
        if (!chatModels.length) return;

        var select = $('modelSelect');
        var current = select.value;
        select.innerHTML = '';

        var def = document.createElement('option');
        def.value = '';
        def.textContent = '默认';
        select.appendChild(def);

        chatModels.forEach(function (m) {
          var opt = document.createElement('option');
          opt.value = m.name;
          opt.textContent = m.provider ? m.name + '（' + m.provider + '）' : m.name;
          select.appendChild(opt);
        });
        select.value = current;
      })
      .catch(function () { /* 忽略：保持"默认" */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
