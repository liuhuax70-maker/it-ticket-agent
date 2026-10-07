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
  var STORE_THEME = 'par.theme';
  var MAX_CONV = 50;
  var NEAR_BOTTOM_PX = 90;

  var SUGGESTIONS = [
    { q: '入职体检费用怎么报销？', tip: '福利 · 报销流程与上限' },
    { q: '年假有多少天？', tip: '考勤休假 · 按司龄分档' },
    { q: '入职体检报销需要在多久内提交？', tip: '查找具体时限' },
    { q: '公司年会在哪家酒店举办？', tip: '文档中不存在，应拒答' }
  ];

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
    section.querySelector('h1').textContent = greeting() + '，有什么可以帮你的？';

    var grid = document.createElement('div');
    grid.className = 'suggestions';
    SUGGESTIONS.forEach(function (item) {
      var btn = document.createElement('button');
      btn.className = 'suggestion';
      btn.type = 'button';
      btn.innerHTML = '<span class="sug-q"></span><span class="sug-tip"></span>';
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

    fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query }),
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
        if (data.model) $('modelChip').textContent = '模型：' + data.model;
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

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    var dark = theme === 'dark';
    $('themeIcon').textContent = dark ? '☀️' : '🌙';
    $('themeText').textContent = dark ? '浅色模式' : '深色模式';
    try { localStorage.setItem(STORE_THEME, theme); } catch (err) { /* 忽略 */ }
  }

  function wireTheme() {
    var saved = null;
    try { saved = localStorage.getItem(STORE_THEME); } catch (err) { /* 忽略 */ }
    var prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(saved || (prefersDark ? 'dark' : 'light'));

    $('themeToggle').addEventListener('click', function () {
      applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
    });
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

  // ---------------- 启动 ----------------

  function init() {
    loadConversations();
    if (conversations.length) currentId = conversations[0].id;
    else newConversation();

    wireTheme();
    wireSidebar();
    wireInput();
    wireCitations();
    wireScroll();
    renderSidebar();
    renderMessages();
    $('input').focus();

    // 顶栏显示当前生效模型（失败时静默，不影响问答）
    fetch('/admin/models')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (models) {
        if (models && models.length) $('modelChip').textContent = '模型：' + models[0].name;
      })
      .catch(function () { /* 忽略 */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
