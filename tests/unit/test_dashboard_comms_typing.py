import pytest
from unittest import mock
import json

# Import the module under test
from static.app import comms as comms_module

# Helper to set up DOM stubs

def setup_dom():
    # Stub document and its methods
    class DummyEl:
        def __init__(self):
            self.classList = mock.Mock()
            self.children = []
            self.style = mock.Mock()
            self.dataset = {}
            self.innerHTML = ""
        def appendChild(self, child):
            self.children.append(child)
        def setAttribute(self, name, value):
            pass
        def addEventListener(self, *args, **kwargs):
            pass
        def querySelectorAll(self, *args, **kwargs):
            return []
    global document
    document = mock.Mock()
    document.getElementById = mock.Mock(side_effect=lambda id: {
        'comms-body': DummyEl(),
        'comms-thread': DummyEl(),
        'comms-landing': DummyEl(),
        'on-air': DummyEl(),
        'comms-send': DummyEl(),
        'comms-input': DummyEl(),
        'comms-trace-toggle': DummyEl(),
    }.get(id))
    document.createElement = mock.Mock(side_effect=lambda tag: DummyEl())
    document.querySelectorAll = mock.Mock(return_value=[])
    # Stub globalThis for localStorage
    class GlobalThis:
        def __init__(self):
            self.localStorage = {}
    global globalThis
    globalThis = GlobalThis()
    # Stub window.matchMedia for reduced motion
    class Window:
        def matchMedia(self, query):
            return type('obj', (object,), {'matches': False})()
    global window
    window = Window()

# Test memoization

def test_typing_indicator_memoization():
    setup_dom()
    # First call, body exists
    el1 = comms_module._typingIndicatorEl()
    assert el1 is not None
    # Second call should return same element
    el2 = comms_module._typingIndicatorEl()
    assert el2 is el1
    # Ensure getElementById called once for body
    assert document.getElementById.call_count == 1
    # Ensure appendChild called once
    body = document.getElementById('comms-body')
    assert body.appendChild.call_count == 1

# Test null body

def test_typing_indicator_null_body():
    # Override getElementById to return None
    document.getElementById = mock.Mock(return_value=None)
    el = comms_module._typingIndicatorEl()
    assert el is None

# Test reduced motion

def test_typing_indicator_reduced_motion():
    # Set reduced motion true
    window.matchMedia = lambda q: type('obj', (object,), {'matches': True})()
    el = comms_module._typingIndicatorEl()
    assert '<span class="comms-typing-static">tower is typing…</span>' in el.innerHTML
    assert 'comms-typing-dot' not in el.innerHTML

# Test full sendCommsMessage happy path

def test_send_comms_message_happy_path():
    setup_dom()
    # Mock fetchJson to return a simple response
    comms_module.fetchJson = mock.Mock(return_value=mock.Mock(json=lambda: {'text': 'reply'}))
    comms_module.sendCommsMessage('hello')
    # Check that two messages appended to thread
    thread = document.getElementById('comms-thread')
    assert len(thread.children) == 2
    # Check that classes appended are 'msg tower' and 'msg tower'
    assert thread.children[0].className == 'msg tower'
    assert thread.children[1].className == 'msg tower'

# Negative test: reduced motion no dots

def test_typing_indicator_no_dots_when_reduced():
    window.matchMedia = lambda q: type('obj', (object,), {'matches': True})()
    el = comms_module._typingIndicatorEl()
    assert 'comms-typing-dot' not in el.innerHTML
