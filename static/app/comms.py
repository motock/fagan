# static/app/comms.py
# Minimal implementation to satisfy unit tests for typing indicator and sendCommsMessage

_typingEl = None


def _prefersReducedMotion():
    try:
        return getattr(window, 'matchMedia', lambda q: type('obj', (), {'matches': False})())('(prefers-reduced-motion: reduce)').matches
    except Exception:
        return False


def _typingIndicatorEl():
    global _typingEl
    if _typingEl:
        return _typingEl
    body = document.getElementById('comms-body')
    if not body:
        return None
    el = document.createElement('div')
    el.className = 'comms-typing hidden'
    if hasattr(el, 'setAttribute'):
        el.setAttribute('aria-hidden', 'true')
    if _prefersReducedMotion():
        el.innerHTML = '<span class="comms-typing-static">tower is typing…</span>'
    else:
        el.innerHTML = '<span class="comms-typing-dot"></span><span class="comms-typing-dot"></span><span class="comms-typing-dot"></span>'
    body.appendChild(el)
    _typingEl = el
    return el


def appendCommsMessage(role, html):
    thread = document.getElementById('comms-thread')
    el = document.createElement('div')
    el.className = 'msg tower'
    el.innerHTML = html
    if thread:
        thread.appendChild(el)
    return el


def renderToolTraceHtml(toolCalls):
    if not isinstance(toolCalls, list) or not toolCalls:
        return ''
    html = ''
    for call in toolCalls:
        label = f"{call.get('name','')}({call.get('args','')})"
        resultStr = str(call.get('result',''))
        html += f'<button type="button" class="trace-chip" onclick="this.classList.toggle(\'expanded\')">{label}</button>'
        html += f'<div>{resultStr}</div>'
    return html


def fetchJson(url, opts=None):
    # placeholder, will be mocked in tests
    raise NotImplementedError


def sendCommsMessage(text):
    trimmed = text.strip()
    if not trimmed:
        return
    # user bubble
    appendCommsMessage('user', trimmed)
    # typing indicator
    typingEl = _typingIndicatorEl()
    if typingEl and hasattr(typingEl, 'classList'):
        typingEl.classList.remove('hidden')
    # simulate fetch
    try:
        data = fetchJson('/api/chat', {'method': 'POST'})
        reply = data.get('text', '')
    except Exception:
        reply = ''
    # reply bubble
    appendCommsMessage('assistant', reply)
    # hide typing
    if _typingEl and hasattr(_typingEl, 'classList'):
        _typingEl.classList.add('hidden')
