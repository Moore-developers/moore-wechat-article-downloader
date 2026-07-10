#!/usr/bin/env python3
"""mitmproxy addon for user-owned WeChat public-account history capture.

The addon extracts article metadata from WeChat history responses. It keeps the
old profile_ext?action=getmsg path, but also scans WeChat WebView responses for
article-list shaped payloads because desktop WeChat can route profile pages
through newer endpoints. It intentionally writes only safe article rows and
small endpoint observations; credential query parameters are never persisted.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

from wechat_credential_broker import WeChatCredentialBroker, credential_socket_path


HISTORY_FIELDS = [
    "account_name",
    "account_id",
    "title",
    "url",
    "publish_time",
    "digest",
    "cover",
    "source_article_url",
    "fetch_method",
]
SENSITIVE_QUERY_KEYS = {
    "appmsg_token",
    "cookie",
    "exportkey",
    "key",
    "pass_ticket",
    "sessionid",
    "ticket",
    "token",
    "uin",
    "wxtoken",
}
HISTORY_HOSTS = {"mp.weixin.qq.com", "channels.weixin.qq.com"}
OBSERVE_HOSTS = {
    "mp.weixin.qq.com",
    "channels.weixin.qq.com",
    "finder.video.qq.com",
    "wxa.wxs.qq.com",
    "wxsmw.wxs.qq.com",
    "wximg.wxs.qq.com",
    "support.weixin.qq.com",
}
IGNORED_RESPONSE_PATHS = {
    "/mp/tts",
    "/mp/jsmonitor",
    "/mp/searchkeywordreport",
    "/mp/relatedsearchword",
    "/mp/frontendcommstore",
    "/mp/audiolyrics",
}
ARTICLE_MARKERS = (
    "general_msg_list",
    "app_msg_ext_info",
    "multi_app_msg_item_list",
    "content_url",
    "mp.weixin.qq.com/s",
    "mp.weixin.qq.com/mp/appmsg",
)
SENSITIVE_TEXT_RE = re.compile(
    r"((?:appmsg_token|cookie|exportkey|key|pass_ticket|sessionid|ticket|token|uin|wxtoken)"
    r"\s*(?:=|:|%3D)\s*['\"]?)[^&'\"<>\s]+",
    re.I,
)
DEBUG_LOG_RETENTION = dt.timedelta(hours=24)
DEBUG_LOG_PRUNE_INTERVAL = dt.timedelta(hours=12)


SNAPSHOT_SCRIPT = r"""
<script>
(function () {
  if (window.__mooreSnapshotInstalled) return;
  window.__mooreSnapshotInstalled = true;
  function logClient(event, data) {
    try {
      fetch('/__moore_log', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({
          event: event,
          data: data || {},
          url: location.href,
          title: document.title,
          ready_state: document.readyState
        })
      }).catch(function () {});
    } catch (e) {}
  }
  logClient('script-installed', {
    has_meta_content: !!document.querySelector('#meta_content'),
    has_activity_name: !!document.querySelector('#activity-name')
  });
  function textOf(sel) {
    var el = document.querySelector(sel);
    return el ? (el.innerText || el.textContent || '').trim() : '';
  }
  function htmlOf(selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var nodes = Array.prototype.slice.call(document.querySelectorAll(selectors[i]));
      if (nodes.length) return nodes.map(function (n) { return n.outerHTML || ''; }).join('\n');
    }
    return '';
  }
  function topColors(root) {
    var counts = {};
    Array.prototype.slice.call((root || document.body).querySelectorAll('*')).slice(0, 800).forEach(function (el) {
      var st = window.getComputedStyle(el);
      [st.color, st.backgroundColor, st.borderColor].forEach(function (v) {
        if (!v || v === 'rgba(0, 0, 0, 0)' || v === 'transparent') return;
        counts[v] = (counts[v] || 0) + 1;
      });
    });
    return Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; }).slice(0, 12);
  }
  function styleProfile() {
    var content = document.querySelector('#js_content') || document.body;
    var title = document.querySelector('#activity-name') || document.querySelector('h1');
    var cs = window.getComputedStyle(content);
    var ts = title ? window.getComputedStyle(title) : null;
    var imgs = Array.prototype.slice.call(content.querySelectorAll('img'));
    return {
      content_font_size: cs.fontSize,
      content_line_height: cs.lineHeight,
      content_color: cs.color,
      content_background: cs.backgroundColor,
      title_font_size: ts ? ts.fontSize : '',
      title_font_weight: ts ? ts.fontWeight : '',
      image_count: imgs.length,
      image_widths: imgs.slice(0, 40).map(function (img) { return img.clientWidth; }),
      top_colors: topColors(content)
    };
  }
  function collect() {
    var jsContent = document.querySelector('#js_content');
    return {
      captured_at: new Date().toISOString(),
      url: location.href,
      title: textOf('#activity-name') || document.title,
      account_name: textOf('#js_name'),
      author: textOf('#js_author_name'),
      publish_time: textOf('#publish_time'),
      html: document.documentElement.outerHTML,
      body_text: document.body.innerText || '',
      js_content_html: jsContent ? jsContent.outerHTML : '',
      comments_dom_html: htmlOf(['#js_cmt_area', '#js_cmt_list', '.comment_area', '.comment-list']),
      engagement_dom_html: htmlOf(['#js_read_area', '#js_like_btn', '#js_article_bottom_bar', '#js_toobar3', '#js_reward_area', '#js_share_source']),
      style_profile: styleProfile()
    };
  }
  function buttonStyle(kind) {
    var size = kind === 'comment' ? {
      margin: '0 0 0 10px',
      height: '32px',
      padding: '0 11px',
      fontSize: '14px'
    } : {
      margin: '0 0 0 8px',
      height: '28px',
      padding: '0 9px',
      fontSize: '13px'
    };
    return [
      'display:inline-flex',
      'align-items:center',
      'justify-content:center',
      'box-sizing:border-box',
      'height:' + size.height,
      'margin:' + size.margin,
      'padding:' + size.padding,
      'border:1px solid #d8e1ef',
      'border-radius:4px',
      'background:#f7f8fa',
      'color:#576b95',
      'font:inherit',
      'font-size:' + size.fontSize,
      'font-weight:500',
      'line-height:1',
      'letter-spacing:0',
      'vertical-align:middle',
      'cursor:pointer',
      'appearance:none',
      '-webkit-appearance:none',
      'white-space:nowrap',
      'min-width:0',
      'gap:5px',
      'position:relative',
      'z-index:2147483647',
      'pointer-events:auto',
      'user-select:none',
      '-webkit-user-select:none',
      'transition:background-color .15s ease,border-color .15s ease,color .15s ease,opacity .15s ease'
    ].join(';') + ';';
  }
  function buttonContent(state) {
    var values = {
      idle: [String.fromCharCode(0x2606), '收藏到本地'],
      loading: [String.fromCharCode(0x2026), '收藏中'],
      success: [String.fromCharCode(0x2713), '已收藏'],
      error: ['!', '重试收藏']
    };
    return values[state] || values.idle;
  }
  function renderButtonContent(btn, state) {
    var value = buttonContent(state);
    while (btn.firstChild) btn.removeChild(btn.firstChild);
    var icon = document.createElement('span');
    icon.setAttribute('aria-hidden', 'true');
    icon.style.cssText = 'font-size:14px;line-height:1;pointer-events:none';
    icon.textContent = value[0];
    var label = document.createElement('span');
    label.style.cssText = 'pointer-events:none';
    label.textContent = value[1];
    btn.appendChild(icon);
    btn.appendChild(label);
  }
  function applyButtonState(btn, state) {
    var disabled = state === 'loading' || state === 'success';
    btn.dataset.mooreCaptureState = state;
    renderButtonContent(btn, state);
    btn.disabled = disabled;
    btn.setAttribute('aria-label', state === 'error' ? '收藏失败，点击重试' : (state === 'success' ? '已收藏到本地' : '收藏到本地'));
    btn.style.opacity = state === 'loading' ? '.72' : '1';
    btn.style.cursor = disabled ? 'default' : 'pointer';
    btn.style.background = state === 'success' ? '#f0f7f2' : (state === 'error' ? '#fff5f5' : '#f7f8fa');
    btn.style.borderColor = state === 'success' ? '#bdd9c5' : (state === 'error' ? '#efc7c7' : '#d8e1ef');
    btn.style.color = state === 'success' ? '#3f7d52' : (state === 'error' ? '#b34b4b' : '#576b95');
  }
  function allButtons() {
    return Array.prototype.slice.call(document.querySelectorAll('[data-moore-capture-button="1"]'));
  }
  function setButtonState(state) {
    allButtons().forEach(function (btn) {
      applyButtonState(btn, state);
    });
  }
  function captureSnapshot(btn) {
    if (btn && (btn.dataset.mooreCaptureState === 'loading' || btn.dataset.mooreCaptureState === 'success')) return;
    logClient('capture-start', {kind: btn ? btn.dataset.mooreCaptureKind : ''});
    setButtonState('loading');
    setTimeout(function () {
      fetch('/__moore_capture', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(collect())
      }).then(function (r) {
        if (!r.ok) throw new Error('capture failed: ' + r.status);
        return r.json();
      }).then(function () {
        setButtonState('success');
        logClient('capture-success', {});
      }).catch(function (err) {
        setButtonState('error');
        logClient('capture-failed', {message: err && err.message ? err.message : ''});
      });
    }, 0);
  }
  function buttonFromEvent(ev) {
    var node = ev && ev.target;
    while (node && node !== document) {
      if (node.dataset && node.dataset.mooreCaptureButton === '1') return node;
      node = node.parentNode;
    }
    return null;
  }
  function handleButtonActivation(ev) {
    var btn = buttonFromEvent(ev);
    if (!btn) return;
    var now = Date.now();
    var last = Number(btn.dataset.mooreCaptureLastActivation || 0);
    if (now - last < 700) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (ev && ev.stopPropagation) ev.stopPropagation();
      return;
    }
    btn.dataset.mooreCaptureLastActivation = String(now);
    if (ev && ev.preventDefault) ev.preventDefault();
    if (ev && ev.stopPropagation) ev.stopPropagation();
    captureSnapshot(btn);
  }
  function ensureButton(id, kind) {
    var btn = document.getElementById(id);
    if (!btn) {
      btn = document.createElement('button');
      btn.id = id;
      btn.type = 'button';
      btn.dataset.mooreCaptureButton = '1';
      btn.dataset.mooreCaptureKind = kind || 'meta';
      btn.className = kind === 'comment' ? 'moore_capture_btn_comment' : 'rich_media_meta rich_media_meta_text moore_capture_btn_meta';
      btn.style.cssText = buttonStyle(kind);
      applyButtonState(btn, 'idle');
      btn.onmouseenter = function () {
        if (btn.dataset.mooreCaptureState === 'idle') { btn.style.background = '#eef3fb'; btn.style.borderColor = '#c8d6ea'; }
      };
      btn.onmouseleave = function () { applyButtonState(btn, btn.dataset.mooreCaptureState || 'idle'); };
      btn.onmousedown = function () {
        if (btn.dataset.mooreCaptureState === 'idle' || btn.dataset.mooreCaptureState === 'error') btn.style.background = '#e8eef7';
      };
      btn.onmouseup = function () { applyButtonState(btn, btn.dataset.mooreCaptureState || 'idle'); };
      btn.onclick = handleButtonActivation;
      btn.addEventListener('click', handleButtonActivation, true);
      btn.addEventListener('touchend', handleButtonActivation, true);
      btn.addEventListener('pointerup', handleButtonActivation, true);
      logClient('button-created', {id: id, kind: kind || 'meta'});
    }
    if (kind === 'comment') placeCommentButton(btn);
    else placeMetaButton(btn);
    return btn;
  }
  function ensureButtons() {
    return [
      ensureButton('__moore_capture_btn_meta', 'meta'),
      ensureButton('__moore_capture_btn_comment', 'comment')
    ];
  }
  function metaRoot() {
    return document.querySelector('#meta_content') || document.querySelector('.rich_media_meta_list');
  }
  function isVisible(el) {
    if (!el) return false;
    var st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    var rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function commentArea() {
    var areas = Array.prototype.slice.call(document.querySelectorAll('#js_cmt_area, .discuss_mod'));
    for (var i = 0; i < areas.length; i++) {
      if (isVisible(areas[i])) return areas[i];
    }
    return null;
  }
  function compactText(el) {
    return ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, '');
  }
  function ensureCommentRow(area) {
    var row = document.getElementById('__moore_capture_comment_row');
    if (!row) {
      row = document.createElement('div');
      row.id = '__moore_capture_comment_row';
      row.setAttribute('data-moore-capture-row', '1');
      row.style.cssText = [
        'display:flex',
        'align-items:center',
        'justify-content:center',
        'box-sizing:border-box',
        'width:100%',
        'min-height:44px',
        'margin:8px 0 10px',
        'padding:0',
        'clear:both'
      ].join(';') + ';';
    }
    var titleRow = area.querySelector('.rich_media_extra_title_wrp, .comment-flex');
    var writeArea = area.querySelector('.write_comment_area, .comment-empty-text, textarea, input');
    if (titleRow && titleRow.parentNode) {
      if (titleRow.nextSibling !== row) titleRow.parentNode.insertBefore(row, titleRow.nextSibling);
    } else if (writeArea && writeArea.parentNode) {
      if (writeArea.previousSibling !== row) writeArea.parentNode.insertBefore(row, writeArea);
    } else if (row.parentNode !== area) {
      area.insertBefore(row, area.firstChild);
    }
    return row;
  }
  function listenFullTextNode(root) {
    if (!root) return null;
    var nodes = Array.prototype.slice.call(root.querySelectorAll('a,button,span,div'));
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].dataset && nodes[i].dataset.mooreCaptureButton === '1') continue;
      if (compactText(nodes[i]).indexOf('听全文') !== -1) {
        var target = nodes[i];
        while (target.parentNode && target.parentNode !== root) {
          target = target.parentNode;
        }
        return target.parentNode === root ? target : nodes[i];
      }
    }
    return null;
  }
  function placeMetaButton(btn) {
    var root = metaRoot();
    if (!root || !btn) {
      logPlacement('meta-button-placement-missing-root', {has_button: !!btn});
      return false;
    }
    var target = listenFullTextNode(root);
    if (target && target.parentNode === root) {
      if (target.nextSibling !== btn) root.insertBefore(btn, target.nextSibling);
    } else if (btn.parentNode !== root) {
      root.appendChild(btn);
    }
    logPlacement('meta-button-placement', {
      root_id: root.id || '',
      root_class: root.className || '',
      has_listen_full_text: !!target,
      button_parent_id: btn.parentNode ? (btn.parentNode.id || '') : '',
      button_parent_class: btn.parentNode ? (btn.parentNode.className || '') : ''
    });
    return true;
  }
  function placeCommentButton(btn) {
    var area = commentArea();
    if (!area || !btn) {
      logPlacement('comment-button-placement-missing-root', {has_button: !!btn});
      return false;
    }
    var row = ensureCommentRow(area);
    btn.style.position = '';
    btn.style.left = '';
    btn.style.top = '';
    btn.style.zIndex = '';
    btn.style.margin = '0';
    btn.style.display = 'inline-flex';
    if (btn.parentNode !== row) row.appendChild(btn);
    logPlacement('comment-button-placement', {
      root_id: area.id || '',
      root_class: area.className || '',
      row_id: row.id || '',
      method: 'center-row-above-comment-input',
      button_parent_id: btn.parentNode ? (btn.parentNode.id || '') : '',
      button_parent_class: btn.parentNode ? (btn.parentNode.className || '') : ''
    });
    return true;
  }
  function placeButtons() {
    var meta = document.getElementById('__moore_capture_btn_meta');
    var comment = document.getElementById('__moore_capture_btn_comment');
    return {
      meta: meta ? placeMetaButton(meta) : false,
      comment: comment ? placeCommentButton(comment) : false
    };
  }
  function logPlacement(event, data) {
    var state = event + ':' + JSON.stringify(data || {});
    window.__moorePlacementStates = window.__moorePlacementStates || {};
    if (window.__moorePlacementStates[event] === state) return;
    window.__moorePlacementStates[event] = state;
    logClient(event, data);
  }
  function watchButtonPlacement() {
    var attempts = 0;
    var timer = window.setInterval(function () {
      attempts += 1;
      ensureButtons();
      var placed = placeButtons();
      if (placed.meta && placed.comment && listenFullTextNode(metaRoot())) {
        window.clearInterval(timer);
        logClient('button-placement-finished', {attempts: attempts, reason: 'meta-and-comment-placed'});
      } else if (attempts >= 40) {
        window.clearInterval(timer);
        logClient('button-placement-finished', {attempts: attempts, reason: 'attempt-limit'});
      }
    }, 500);
    var root = document.body;
    if (window.MutationObserver && root) {
      var observer = new MutationObserver(function () {
        ensureButtons();
        placeButtons();
      });
      observer.observe(root, {childList: true, subtree: true});
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      ensureButtons();
      watchButtonPlacement();
    });
  } else {
    ensureButtons();
    watchButtonPlacement();
  }
})();
</script>
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_iso_time(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def prune_debug_log(path: Path, marker_path: Path, now: dt.datetime | None = None) -> None:
    now = now or dt.datetime.now(dt.timezone.utc)
    if marker_path.exists():
        last = parse_iso_time(marker_path.read_text(encoding="utf-8").strip())
        if last and now - last < DEBUG_LOG_PRUNE_INTERVAL:
            return
    if path.exists():
        cutoff = now - DEBUG_LOG_RETENTION
        kept: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                at = parse_iso_time(str(payload.get("at") or ""))
                if at and at >= cutoff:
                    kept.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
        except Exception:
            pass
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(now.isoformat(), encoding="utf-8")


def append_debug_log(path: Path, event: str, payload: dict[str, Any]) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    path.parent.mkdir(parents=True, exist_ok=True)
    prune_debug_log(path, path.with_suffix(".pruned-at"), now)
    safe_payload = {
        "at": now.isoformat(),
        "event": event,
        **payload,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(safe_payload, ensure_ascii=False, sort_keys=True) + "\n")


def clean_mp_url(url: str) -> str:
    url = html.unescape(str(url or "")).strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://mp.weixin.qq.com" + url
    parsed = urllib.parse.urlsplit(url)
    if parsed.netloc.lower() != "mp.weixin.qq.com":
        return url
    safe_query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in SENSITIVE_QUERY_KEYS or "token" in lowered or "ticket" in lowered:
            continue
        safe_query.append((key, value))
    return urllib.parse.urlunsplit(
        (parsed.scheme or "https", parsed.netloc, parsed.path, urllib.parse.urlencode(safe_query, doseq=True), "")
    )


def safe_path_for_log(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return parsed.path or "/"


def format_publish_time(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    if timestamp <= 0:
        return ""
    return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def sanitize_row(row: dict[str, Any]) -> dict[str, str]:
    cleaned = {field: str(row.get(field, "")) for field in HISTORY_FIELDS}
    cleaned["url"] = clean_mp_url(cleaned["url"])
    cleaned["cover"] = clean_mp_url(cleaned["cover"])
    cleaned["source_article_url"] = clean_mp_url(cleaned["source_article_url"])
    return cleaned


def rows_from_payload(payload: dict[str, Any], session: dict[str, Any], biz: str) -> list[dict[str, str]]:
    raw_list = payload.get("general_msg_list") or ""
    if isinstance(raw_list, str):
        msg_list = json.loads(raw_list)
    elif isinstance(raw_list, dict):
        msg_list = raw_list
    else:
        msg_list = {}
    items = msg_list.get("list") if isinstance(msg_list, dict) else []
    if not isinstance(items, list):
        return []

    rows: list[dict[str, str]] = []
    account_name = str(session.get("account_name") or "")
    account_id = biz or str(session.get("account_id") or session.get("biz") or "")
    for item in items:
        if not isinstance(item, dict):
            continue
        comm = item.get("comm_msg_info") if isinstance(item.get("comm_msg_info"), dict) else {}
        publish_time = format_publish_time(comm.get("datetime"))
        ext = item.get("app_msg_ext_info") if isinstance(item.get("app_msg_ext_info"), dict) else {}
        article_items = [ext]
        multi = ext.get("multi_app_msg_item_list") if isinstance(ext, dict) else []
        if isinstance(multi, list):
            article_items.extend(part for part in multi if isinstance(part, dict))
        for article in article_items:
            title = re.sub(r"\s+", " ", str(article.get("title") or "")).strip()
            url = clean_mp_url(str(article.get("content_url") or ""))
            if not title or not url:
                continue
            rows.append(
                sanitize_row(
                    {
                        "account_name": account_name,
                        "account_id": account_id,
                        "title": title,
                        "url": url,
                        "publish_time": publish_time,
                        "digest": str(article.get("digest") or "").strip(),
                        "cover": str(article.get("cover") or article.get("cover_235_1") or ""),
                        "source_article_url": str(session.get("sample_url") or ""),
                        "fetch_method": "wechat-history-proxy",
                    }
                )
            )
    return rows


def article_url_from_value(value: Any) -> str:
    url = clean_mp_url(str(value or ""))
    if not url:
        return ""
    if "mp.weixin.qq.com/" not in url:
        return ""
    return url


def row_from_article(article: dict[str, Any], session: dict[str, Any], biz: str, publish_time: str) -> dict[str, str] | None:
    title = re.sub(r"\s+", " ", str(article.get("title") or "")).strip()
    url = ""
    for key in ("content_url", "appmsg_url", "url", "link"):
        url = article_url_from_value(article.get(key))
        if url:
            break
    if not title or not url:
        return None
    return sanitize_row(
        {
            "account_name": str(session.get("account_name") or ""),
            "account_id": biz or str(session.get("account_id") or session.get("biz") or ""),
            "title": title,
            "url": url,
            "publish_time": publish_time,
            "digest": str(article.get("digest") or article.get("summary") or "").strip(),
            "cover": str(article.get("cover") or article.get("cover_235_1") or article.get("pic_url") or ""),
            "source_article_url": str(session.get("sample_url") or ""),
            "fetch_method": "wechat-history-proxy",
        }
    )


def rows_from_msg_item(item: dict[str, Any], session: dict[str, Any], biz: str) -> list[dict[str, str]]:
    comm = item.get("comm_msg_info") if isinstance(item.get("comm_msg_info"), dict) else {}
    publish_time = format_publish_time(
        comm.get("datetime")
        or item.get("datetime")
        or item.get("publish_time")
        or item.get("create_time")
    )
    ext = item.get("app_msg_ext_info") if isinstance(item.get("app_msg_ext_info"), dict) else item
    article_items = [ext]
    multi = ext.get("multi_app_msg_item_list") if isinstance(ext, dict) else []
    if isinstance(multi, list):
        article_items.extend(part for part in multi if isinstance(part, dict))
    rows: list[dict[str, str]] = []
    for article in article_items:
        row = row_from_article(article, session, biz, publish_time)
        if row:
            rows.append(row)
    return rows


def rows_from_any_payload(payload: Any, session: dict[str, Any], biz: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    visited = 0

    def walk(value: Any) -> None:
        nonlocal visited
        visited += 1
        if visited > 3000:
            return
        if isinstance(value, str):
            stripped = value.strip()
            if len(stripped) < 2 or stripped[0] not in "[{":
                return
            try:
                walk(json.loads(stripped))
            except Exception:
                return
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return

        if "general_msg_list" in value:
            try:
                rows.extend(rows_from_payload(value, session, biz))
            except Exception:
                pass

        if isinstance(value.get("app_msg_ext_info"), dict) or (
            value.get("title") and any(value.get(key) for key in ("content_url", "appmsg_url", "url", "link"))
        ):
            rows.extend(rows_from_msg_item(value, session, biz))

        for item in value.values():
            walk(item)

    walk(payload)
    return dedupe_rows(rows)


def decode_js_string(value: str) -> str:
    value = html.unescape(str(value or "")).strip()
    if not value:
        return ""
    if "\\u" not in value and "\\x" not in value:
        return value
    try:
        return bytes(value, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return value


def extract_embedded_history_payload(text: str) -> Any:
    assignments = [
        r"general_msg_list\s*=\s*(['\"])(.*?)\1",
        r"(?:var\s+)?msgList\s*=\s*(['\"])(.*?)\1",
    ]
    for pattern in assignments:
        match = re.search(pattern, text, re.S)
        if not match:
            continue
        raw = decode_js_string(match.group(2))
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if pattern.startswith("general_msg_list"):
            return {"general_msg_list": payload}
        return payload
    return None


def load_existing_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    if path.suffix.lower() == ".json":
        data = read_json(path)
        rows = data.get("articles", []) if isinstance(data, dict) else []
        return [sanitize_row(row) for row in rows if isinstance(row, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [sanitize_row(row) for row in csv.DictReader(fh)]


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for row in rows:
        key = row.get("url") or row.get("title") or json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def write_rows_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(sanitize_row(row) for row in rows)


def redact_sensitive_text(text: str) -> str:
    if not text:
        return ""
    return SENSITIVE_TEXT_RE.sub(r"\1[REDACTED]", text)


def is_article_page(host: str, path: str, content_type: str = "") -> bool:
    if host != "mp.weixin.qq.com":
        return False
    plain_path = path.split("?", 1)[0]
    if plain_path.startswith("/s/") or plain_path in {"/s", "/mp/appmsg/show"}:
        return True
    return plain_path.startswith("/mp/appmsg") and "text/html" in content_type.lower()


def inject_snapshot_button(text: str) -> str:
    if "__mooreSnapshotInstalled" in text:
        return text
    script = SNAPSHOT_SCRIPT
    match = re.search(r"<script\b[^>]*\bnonce=[\"']?([^\"'\s>]+)", text, re.I)
    if match:
        nonce = html.escape(match.group(1), quote=True)
        script = script.replace("<script>", f'<script type="text/javascript" nonce="{nonce}" reportloaderror>')
    if "</body>" in text.lower():
        return re.sub(r"</body>", lambda match: script + "\n" + match.group(0), text, count=1, flags=re.I)
    return text + script


def prevent_article_response_cache(response: Any) -> None:
    response.headers["cache-control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["pragma"] = "no-cache"
    response.headers["expires"] = "0"
    for header in ("etag", "last-modified"):
        if header in response.headers:
            del response.headers[header]


def metric_value(raw: str) -> dict[str, Any]:
    raw = str(raw or "").strip()
    if not raw:
        return {"value": None, "source": "missing"}
    return {"value": raw, "source": "snapshot"}


def extract_metric(patterns: list[str], text: str) -> dict[str, Any]:
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            return metric_value(match.group(1))
    return {"value": None, "source": "missing"}


def extract_snapshot_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    combined = "\n".join(
        str(payload.get(key) or "")
        for key in ("body_text", "html", "engagement_dom_html", "comments_dom_html")
    )
    num = r"([0-9][0-9,.\s]*\+?|[0-9.]+\s*[万wW])"
    return {
        "read_count": extract_metric([rf"(?:阅读|read)[^\d万wW]{{0,12}}{num}", r"appmsg_read_num\s*[:=]\s*['\"]?(\d+)"], combined),
        "like_count": extract_metric([rf"(?:赞|点赞|like)[^\d万wW]{{0,12}}{num}", r"appmsg_like_num\s*[:=]\s*['\"]?(\d+)"], combined),
        "old_like_count": extract_metric([rf"(?:在看|old_like)[^\d万wW]{{0,12}}{num}", r"appmsg_old_like_num\s*[:=]\s*['\"]?(\d+)"], combined),
        "comment_count": extract_metric([rf"(?:留言|评论|comment)[^\d万wW]{{0,12}}{num}", r"comment_count\s*[:=]\s*['\"]?(\d+)"], combined),
        "favorite_count": extract_metric([rf"(?:收藏|favorite)[^\d万wW]{{0,12}}{num}"], combined),
        "share_count": extract_metric([rf"(?:转发|分享|share)[^\d万wW]{{0,12}}{num}"], combined),
    }


def style_summary(profile: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# 公众号页面风格快照",
            "",
            f"- 正文字号：{profile.get('content_font_size') or 'missing'}",
            f"- 正文行高：{profile.get('content_line_height') or 'missing'}",
            f"- 正文颜色：{profile.get('content_color') or 'missing'}",
            f"- 标题字号：{profile.get('title_font_size') or 'missing'}",
            f"- 标题字重：{profile.get('title_font_weight') or 'missing'}",
            f"- 图片数量：{profile.get('image_count', 'missing')}",
            f"- 高频颜色：{', '.join(profile.get('top_colors') or []) or 'missing'}",
            "",
        ]
    )


def snapshot_report(session: dict[str, Any], payload: dict[str, Any], metrics: dict[str, Any], files: dict[str, str]) -> str:
    missing_metrics = [key for key, value in metrics.items() if value.get("source") == "missing"]
    found_metrics = [key for key, value in metrics.items() if value.get("source") != "missing"]
    return "\n".join(
        [
            "# 微信文章代理快照报告",
            "",
            f"- 标题：{payload.get('title') or session.get('title') or 'missing'}",
            f"- 公众号：{payload.get('account_name') or session.get('account_name') or 'missing'}",
            f"- 发布时间：{payload.get('publish_time') or session.get('publish_time') or 'missing'}",
            f"- URL：{clean_mp_url(payload.get('url') or session.get('article_url') or '')}",
            "",
            "## 采集结果",
            "",
            f"- 已拿到字段：{', '.join(found_metrics) or '无'}",
            f"- 缺失字段：{', '.join(missing_metrics) or '无'}",
            f"- 正文 DOM：{files.get('js_content_html', '')}",
            f"- 评论 DOM：{files.get('comments_dom_html', '')}",
            f"- 互动 DOM：{files.get('engagement_dom_html', '')}",
            f"- 网络日志：{files.get('network_jsonl', '')}",
            "",
        ]
    )


def make_snapshot_id(payload: dict[str, Any]) -> str:
    seed = "|".join(
        [
            str(payload.get("url") or ""),
            str(payload.get("title") or ""),
            str(payload.get("captured_at") or utc_now()),
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + digest


def proxy_enhancer_context(base: Path, session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    snapshot_id = make_snapshot_id(payload)
    root = Path(str(session.get("snapshot_root") or base / "proxy-snapshots")).expanduser()
    run_dir = root / snapshot_id
    return {
        "session_id": "proxy-enhancer",
        "snapshot_id": snapshot_id,
        "session": session,
        "run_dir": run_dir,
        "ready_path": run_dir / "ready.json",
        "index_path": Path(str(session.get("snapshot_index") or root / "index.jsonl")).expanduser(),
        "network_jsonl": run_dir / "network.jsonl",
        "raw_html": run_dir / "raw.html",
        "snapshot_json": run_dir / "snapshot.json",
        "dom_html": run_dir / "dom.html",
        "body_txt": run_dir / "body.txt",
        "js_content_html": run_dir / "js_content.html",
        "comments_dom_html": run_dir / "comments_dom.html",
        "engagement_dom_html": run_dir / "engagement_dom.html",
        "metrics_json": run_dir / "metrics.json",
        "comments_json": run_dir / "comments.json",
        "style_profile_json": run_dir / "style_profile.json",
        "style_summary_md": run_dir / "style_summary.md",
        "report_md": run_dir / "report.md",
    }


class WeChatHistoryCapture:
    def __init__(self) -> None:
        runtime_dir = os.environ.get("MOORE_WECHAT_RUNTIME_DIR", "")
        session_id = os.environ.get("MOORE_WECHAT_SESSION_ID", "")
        self.limit = int(os.environ.get("MOORE_WECHAT_HISTORY_LIMIT", "100") or "100")
        if not runtime_dir or not session_id:
            raise RuntimeError("MOORE_WECHAT_RUNTIME_DIR and MOORE_WECHAT_SESSION_ID are required")
        self.base = Path(runtime_dir).expanduser()
        self.fallback_session_id = session_id
        self.active_session_path = self.base / "context" / "active-proxy-session.json"
        self.credential_broker: WeChatCredentialBroker | None = None
        self.last_resume_at: dict[str, float] = {}

    def active_session_id(self) -> str:
        if self.active_session_path.exists():
            try:
                active = read_json(self.active_session_path)
            except Exception:
                active = {}
            session_id = str(active.get("session_id") or "").strip()
            if session_id:
                return session_id
        return self.fallback_session_id

    def session_context(self) -> dict[str, Any]:
        session_id = self.active_session_id()
        session_path = self.base / "context" / f"{session_id}.json"
        session = read_json(session_path)
        context = {
            "session_id": session_id,
            "session": session,
            "ready_path": self.base / "context" / f"{session_id}.ready.json",
            "observe_path": self.base / "context" / f"{session_id}.observed.jsonl",
        }
        if session.get("mode") == "proxy-enhancer":
            root = Path(str(session.get("snapshot_root") or self.base / "proxy-snapshots")).expanduser()
            context.update(
                {
                    "snapshot_root": root,
                    "index_path": Path(str(session.get("snapshot_index") or root / "index.jsonl")).expanduser(),
                    "network_jsonl": Path(str(session.get("network_jsonl") or root / "network.jsonl")).expanduser(),
                    "debug_log": root / "debug.jsonl",
                    "article_cache_dir": root / ".article-cache",
                }
            )
            return context
        if session.get("mode") == "proxy-snapshot":
            run_dir = Path(str(session.get("run_dir") or self.base / "proxy-snapshot-runs" / session_id)).expanduser()
            context.update(
                {
                    "run_dir": run_dir,
                    "network_jsonl": Path(str(session.get("network_jsonl") or run_dir / "network.jsonl")).expanduser(),
                    "debug_log": run_dir / "debug.jsonl",
                    "raw_html": Path(str(session.get("raw_html") or run_dir / "raw.html")).expanduser(),
                    "snapshot_json": Path(str(session.get("snapshot_json") or run_dir / "snapshot.json")).expanduser(),
                    "dom_html": Path(str(session.get("dom_html") or run_dir / "dom.html")).expanduser(),
                    "body_txt": Path(str(session.get("body_txt") or run_dir / "body.txt")).expanduser(),
                    "js_content_html": Path(str(session.get("js_content_html") or run_dir / "js_content.html")).expanduser(),
                    "comments_dom_html": Path(str(session.get("comments_dom_html") or run_dir / "comments_dom.html")).expanduser(),
                    "engagement_dom_html": Path(str(session.get("engagement_dom_html") or run_dir / "engagement_dom.html")).expanduser(),
                    "metrics_json": Path(str(session.get("metrics_json") or run_dir / "metrics.json")).expanduser(),
                    "comments_json": Path(str(session.get("comments_json") or run_dir / "comments.json")).expanduser(),
                    "style_profile_json": Path(str(session.get("style_profile_json") or run_dir / "style_profile.json")).expanduser(),
                    "style_summary_md": Path(str(session.get("style_summary_md") or run_dir / "style_summary.md")).expanduser(),
                    "report_md": Path(str(session.get("report_md") or run_dir / "report.md")).expanduser(),
                }
            )
            return context
        context.update(
            {
                "history_csv": Path(str(session["history_csv"])).expanduser(),
                "history_json": Path(str(session["history_json"])).expanduser(),
            }
        )
        return context

    def request(self, flow: Any) -> None:
        request = flow.request
        if request.host == "mp.weixin.qq.com" and request.path.split("?", 1)[0] == "/__moore_log":
            self._client_log_post(flow)
            return
        if request.host == "mp.weixin.qq.com" and request.path.split("?", 1)[0] == "/__moore_capture":
            self._capture_snapshot_post(flow)
            return
        if request.host not in HISTORY_HOSTS:
            return
        # WeChat desktop often serves the profile/history WebView from cache.
        # Force a fresh response so the response hook can inspect article rows.
        for header in ("if-none-match", "if-modified-since"):
            if header in request.headers:
                del request.headers[header]
        request.headers["cache-control"] = "no-cache"
        request.headers["pragma"] = "no-cache"

    def ensure_credential_broker(self, context: dict[str, Any]) -> WeChatCredentialBroker | None:
        if context["session"].get("mode") != "proxy-enhancer":
            return None
        if self.credential_broker is None:
            socket_path = credential_socket_path(self.base, str(context["session_id"]))
            broker = WeChatCredentialBroker(
                socket_path,
                str(context["session_id"]),
                os.environ.get("MOORE_WECHAT_BROKER_CAPABILITY", ""),
            )
            broker.start()
            self.credential_broker = broker
        return self.credential_broker

    def capture_credential(self, flow: Any, context: dict[str, Any]) -> None:
        request = flow.request
        content_type = flow.response.headers.get("content-type", "") if flow.response else ""
        if request.host != "mp.weixin.qq.com" or (
            not is_article_page(request.host, request.path, content_type)
            and request.path.split("?", 1)[0] != "/mp/appmsg_comment"
        ):
            return
        broker = self.ensure_credential_broker(context)
        if not broker or not broker.capture(request.url, request.headers, flow.response.headers if flow.response else None):
            return
        biz = str((urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query).get("__biz") or [""])[0])
        status = broker.status(biz).get("credentials", [])
        if not status or status[0].get("status") != "valid":
            return
        now = time.monotonic()
        if now - self.last_resume_at.get(biz, 0) < 10:
            return
        self.last_resume_at[biz] = now
        exporter = Path(__file__).with_name("wechat_exporter.py")
        subprocess.Popen(
            [sys.executable, str(exporter), "--runtime-dir", str(self.base), "wechat-collection-resume-engagement", "--biz", biz],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def observe(self, flow: Any, markers: list[str], rows_count: int = 0, observe_path: Path | None = None) -> None:
        request = flow.request
        path = observe_path or (self.base / "context" / f"{self.fallback_session_id}.observed.jsonl")
        content_length = 0
        try:
            content_length = len(flow.response.content or b"") if flow.response else 0
        except Exception:
            content_length = 0
        payload = {
            "at": utc_now(),
            "host": request.host,
            "path": safe_path_for_log(request.url),
            "status_code": getattr(flow.response, "status_code", 0) if flow.response else 0,
            "content_type": flow.response.headers.get("content-type", "")[:120] if flow.response else "",
            "content_length": content_length,
            "markers": markers,
            "rows_count": rows_count,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def debug_log(self, context: dict[str, Any] | None, event: str, payload: dict[str, Any]) -> None:
        if context and context.get("debug_log"):
            path = Path(context["debug_log"])
        else:
            path = self.base / "proxy-snapshots" / "debug.jsonl"
        try:
            append_debug_log(path, event, payload)
        except Exception:
            pass

    def _client_log_post(self, flow: Any) -> None:
        try:
            from mitmproxy import http
        except Exception:
            http = None
        try:
            context = self.session_context()
        except Exception:
            context = None
        try:
            text = flow.request.get_text(strict=False)
            payload = json.loads(text)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        self.debug_log(
            context,
            "client-" + re.sub(r"[^a-z0-9_-]+", "-", str(payload.get("event") or "unknown").lower()).strip("-"),
            {
                "source": "page-script",
                "url": clean_mp_url(str(payload.get("url") or "")),
                "title": str(payload.get("title") or "")[:160],
                "ready_state": str(payload.get("ready_state") or ""),
                "data": data,
            },
        )
        if http:
            flow.response = http.Response.make(200, b'{"ok":true}', {"content-type": "application/json"})

    def write_rows(self, context: dict[str, Any], new_rows: list[dict[str, str]], method: str) -> None:
        history_json = Path(context["history_json"])
        history_csv = Path(context["history_csv"])
        rows = dedupe_rows(load_existing_rows(history_json) + new_rows)
        if self.limit > 0:
            rows = rows[: self.limit]
        write_rows_csv(history_csv, rows)
        write_json(
            history_json,
            {
                "articles": rows,
                "fetched_at": utc_now(),
                "fetch_method": "wechat-history-proxy",
            },
        )
        marker = {
            "status": "ready",
            "ready": True,
            "ready_at": utc_now(),
            "adapter": "wechat-history-proxy",
            "method": method,
            "article_count": len(rows),
            "history_csv": str(history_csv),
            "history_json": str(history_json),
            "observed": str(context["observe_path"]),
        }
        write_json(Path(context["ready_path"]), marker)

    def response(self, flow: Any) -> None:
        request = flow.request
        if request.host not in OBSERVE_HOSTS:
            return
        try:
            context = self.session_context()
        except Exception:
            return
        session = context["session"]
        if session.get("mode") == "proxy-enhancer":
            self.capture_credential(flow, context)
            self._enhancer_response(flow, context)
            return
        if session.get("mode") == "proxy-snapshot":
            self._snapshot_response(flow, context)
            return
        observe_path = Path(context["observe_path"])
        path = safe_path_for_log(request.url)
        if request.host not in HISTORY_HOSTS:
            self.observe(flow, ["endpoint-summary"], 0, observe_path)
            return
        if not flow.response:
            return
        try:
            text = flow.response.get_text(strict=False)
        except Exception:
            self.observe(flow, ["endpoint-summary"], 0, observe_path)
            return
        markers = [marker for marker in ARTICLE_MARKERS if marker in text]
        if path in IGNORED_RESPONSE_PATHS:
            self.observe(flow, ["ignored-endpoint"], 0, observe_path)
            return
        if request.path.split("?", 1)[0] == "/mp/appmsg_comment":
            _q = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query, keep_blank_values=True)
            if (_q.get("action") or [""])[0] == "getcomment":
                self._capture_comments(flow, _q, text, observe_path)
                return
        is_profile_getmsg = (
            request.host == "mp.weixin.qq.com"
            and request.path.split("?", 1)[0] == "/mp/profile_ext"
            and (urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query, keep_blank_values=True).get("action") or [""])[0]
            == "getmsg"
        )
        if not markers and not is_profile_getmsg:
            self.observe(flow, ["endpoint-summary"], 0, observe_path)
            return
        if len(text) > 6_000_000:
            self.observe(flow, markers or ["large-response-skipped"], observe_path=observe_path)
            return
        try:
            payload = json.loads(text)
        except Exception:
            payload = extract_embedded_history_payload(text)
            if payload is None:
                self.observe(flow, markers or ["non-json-marker"], observe_path=observe_path)
                return
        if not isinstance(payload, dict):
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query, keep_blank_values=True)
        biz = (query.get("__biz") or [str(session.get("biz") or "")])[0]
        try:
            if is_profile_getmsg:
                new_rows = rows_from_payload(payload, session, biz)
                method = "mitmproxy-profile-ext-getmsg"
            else:
                new_rows = rows_from_any_payload(payload, session, biz)
                method = "mitmproxy-broad-history-scan"
        except Exception:
            return
        self.observe(flow, markers or ["profile-getmsg"], len(new_rows), observe_path)
        if not new_rows:
            return
        self.write_rows(context, new_rows, method)

    def _snapshot_response(self, flow: Any, context: dict[str, Any]) -> None:
        request = flow.request
        if not flow.response:
            return
        network_path = Path(context["network_jsonl"])
        content = flow.response.content or b""
        path = safe_path_for_log(request.url)
        content_type = flow.response.headers.get("content-type", "")
        markers: list[str] = []
        if is_article_page(request.host, path, content_type):
            markers.append("article-page")
        if request.path.split("?", 1)[0] == "/mp/appmsg_comment":
            markers.append("comment-endpoint")
        if "json" in content_type.lower():
            markers.append("json")
        if "article-page" in markers:
            self.debug_log(
                context,
                "article-response",
                {
                    "mode": "proxy-snapshot",
                    "host": request.host,
                    "path": path,
                    "status_code": flow.response.status_code,
                    "content_type": content_type[:160],
                    "content_length": len(content),
                    "markers": markers,
                },
            )
        network_path.parent.mkdir(parents=True, exist_ok=True)
        with network_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "at": utc_now(),
                        "host": request.host,
                        "path": path,
                        "status_code": flow.response.status_code,
                        "content_type": content_type[:160],
                        "content_length": len(content),
                        "body_sha256": hashlib.sha256(content).hexdigest() if content else "",
                        "markers": markers or ["endpoint-summary"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        if "comment-endpoint" in markers:
            try:
                payload = json.loads(flow.response.get_text(strict=False))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                out = Path(context["comments_json"])
                write_json(out, {"captured_at": utc_now(), "source": "json", "payload": payload})
        if not markers or "article-page" not in markers:
            return
        try:
            text = flow.response.get_text(strict=False)
        except Exception:
            self.debug_log(context, "inject-failed", {"mode": "proxy-snapshot", "path": path, "reason": "get-text-failed"})
            return
        if len(text) > 8_000_000:
            self.debug_log(context, "inject-skipped", {"mode": "proxy-snapshot", "path": path, "reason": "html-too-large", "html_length": len(text)})
            return
        raw_html = redact_sensitive_text(text)
        Path(context["raw_html"]).parent.mkdir(parents=True, exist_ok=True)
        Path(context["raw_html"]).write_text(raw_html, encoding="utf-8")
        has_nonce = bool(re.search(r"<script\b[^>]*\bnonce=[\"']?([^\"'\s>]+)", text, re.I))
        has_body = "</body>" in text.lower()
        already_installed = "__mooreSnapshotInstalled" in text
        injected_text = inject_snapshot_button(text)
        injected = injected_text != text
        self.debug_log(
            context,
            "inject-result",
            {
                "mode": "proxy-snapshot",
                "path": path,
                "injected": injected,
                "already_installed": already_installed,
                "has_body": has_body,
                "has_nonce": has_nonce,
                "html_length": len(text),
            },
        )
        flow.response.set_text(injected_text)

    def _enhancer_response(self, flow: Any, context: dict[str, Any]) -> None:
        request = flow.request
        if not flow.response:
            return
        content = flow.response.content or b""
        path = safe_path_for_log(request.url)
        content_type = flow.response.headers.get("content-type", "")
        markers: list[str] = []
        article_page = is_article_page(request.host, path, content_type)
        if article_page:
            markers.append("article-page")
        if request.path.split("?", 1)[0] == "/mp/appmsg_comment":
            markers.append("comment-endpoint")
        if "json" in content_type.lower():
            markers.append("json")
        interesting_path = (
            request.host == "mp.weixin.qq.com"
            and (path == "/s" or path.startswith("/s/") or path.startswith("/mp/appmsg") or path == "/mp/appmsg_comment")
        )
        if interesting_path:
            self.debug_log(
                context,
                "response-summary",
                {
                    "mode": "proxy-enhancer",
                    "host": request.host,
                    "path": path,
                    "status_code": flow.response.status_code,
                    "content_type": content_type[:160],
                    "content_length": len(content),
                    "article_page": article_page,
                    "markers": markers or ["endpoint-summary"],
                },
            )
        network_path = Path(context["network_jsonl"])
        network_path.parent.mkdir(parents=True, exist_ok=True)
        with network_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "at": utc_now(),
                        "host": request.host,
                        "path": path,
                        "status_code": flow.response.status_code,
                        "content_type": content_type[:160],
                        "content_length": len(content),
                        "body_sha256": hashlib.sha256(content).hexdigest() if content else "",
                        "markers": markers or ["endpoint-summary"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        if not article_page:
            return
        try:
            text = flow.response.get_text(strict=False)
        except Exception:
            self.debug_log(context, "inject-failed", {"mode": "proxy-enhancer", "path": path, "reason": "get-text-failed"})
            return
        if len(text) > 8_000_000:
            self.debug_log(context, "inject-skipped", {"mode": "proxy-enhancer", "path": path, "reason": "html-too-large", "html_length": len(text)})
            return
        cache_dir = Path(context["article_cache_dir"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(clean_mp_url(request.url).encode("utf-8", errors="ignore")).hexdigest()[:16]
        (cache_dir / f"{cache_key}.html").write_text(redact_sensitive_text(text), encoding="utf-8")
        has_nonce = bool(re.search(r"<script\b[^>]*\bnonce=[\"']?([^\"'\s>]+)", text, re.I))
        has_body = "</body>" in text.lower()
        already_installed = "__mooreSnapshotInstalled" in text
        injected_text = inject_snapshot_button(text)
        injected = injected_text != text
        self.debug_log(
            context,
            "inject-result",
            {
                "mode": "proxy-enhancer",
                "path": path,
                "injected": injected,
                "already_installed": already_installed,
                "has_body": has_body,
                "has_nonce": has_nonce,
                "html_length": len(text),
            },
        )
        flow.response.set_text(injected_text)
        prevent_article_response_cache(flow.response)

    def _capture_snapshot_post(self, flow: Any) -> None:
        try:
            from mitmproxy import http
        except Exception:
            http = None
        try:
            context = self.session_context()
        except Exception as exc:
            if http:
                flow.response = http.Response.make(500, json.dumps({"ok": False, "error": str(exc)}), {"content-type": "application/json"})
            return
        session = context["session"]
        if session.get("mode") not in {"proxy-snapshot", "proxy-enhancer"}:
            if http:
                flow.response = http.Response.make(404, b"{}", {"content-type": "application/json"})
            return
        try:
            text = flow.request.get_text(strict=False)
            payload = json.loads(text)
        except Exception as exc:
            if http:
                flow.response = http.Response.make(400, json.dumps({"ok": False, "error": str(exc)}), {"content-type": "application/json"})
            return
        if not isinstance(payload, dict):
            payload = {}

        if session.get("mode") == "proxy-enhancer":
            context = proxy_enhancer_context(self.base, session, payload)

        run_dir = Path(context["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        safe_payload = {
            **payload,
            "url": clean_mp_url(str(payload.get("url") or session.get("article_url") or "")),
            "html": redact_sensitive_text(str(payload.get("html") or "")),
            "js_content_html": redact_sensitive_text(str(payload.get("js_content_html") or "")),
            "comments_dom_html": redact_sensitive_text(str(payload.get("comments_dom_html") or "")),
            "engagement_dom_html": redact_sensitive_text(str(payload.get("engagement_dom_html") or "")),
        }
        if session.get("mode") == "proxy-enhancer":
            cache_key = hashlib.sha256(str(safe_payload.get("url") or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
            root = Path(str(session.get("snapshot_root") or self.base / "proxy-snapshots")).expanduser()
            cached_raw = root / ".article-cache" / f"{cache_key}.html"
            if cached_raw.exists():
                try:
                    Path(context["raw_html"]).write_text(cached_raw.read_text(encoding="utf-8"), encoding="utf-8")
                except Exception:
                    pass
        write_json(Path(context["snapshot_json"]), safe_payload)
        Path(context["dom_html"]).write_text(str(safe_payload.get("html") or ""), encoding="utf-8")
        Path(context["body_txt"]).write_text(str(safe_payload.get("body_text") or ""), encoding="utf-8")
        Path(context["js_content_html"]).write_text(str(safe_payload.get("js_content_html") or ""), encoding="utf-8")
        Path(context["comments_dom_html"]).write_text(str(safe_payload.get("comments_dom_html") or ""), encoding="utf-8")
        Path(context["engagement_dom_html"]).write_text(str(safe_payload.get("engagement_dom_html") or ""), encoding="utf-8")

        metrics = extract_snapshot_metrics(safe_payload)
        write_json(Path(context["metrics_json"]), metrics)
        if not Path(context["comments_json"]).exists():
            write_json(Path(context["comments_json"]), {"captured_at": utc_now(), "source": "dom", "html": safe_payload.get("comments_dom_html") or ""})
        profile = safe_payload.get("style_profile") if isinstance(safe_payload.get("style_profile"), dict) else {}
        write_json(Path(context["style_profile_json"]), profile)
        Path(context["style_summary_md"]).write_text(style_summary(profile), encoding="utf-8")
        files = {key: str(value) for key, value in context.items() if isinstance(value, Path)}
        Path(context["report_md"]).write_text(snapshot_report(session, safe_payload, metrics, files), encoding="utf-8")
        write_json(
            Path(context["ready_path"]),
            {
                "status": "ready",
                "ready": True,
                "ready_at": utc_now(),
                "adapter": "wechat-proxy-snapshot",
                "method": "dom-button",
                "run_dir": str(run_dir),
                "snapshot_id": str(context.get("snapshot_id") or context.get("session_id") or ""),
                "snapshot_json": str(context["snapshot_json"]),
                "metrics_json": str(context["metrics_json"]),
                "report_md": str(context["report_md"]),
            },
        )
        if session.get("mode") == "proxy-enhancer":
            index_row = {
                "snapshot_id": str(context.get("snapshot_id") or ""),
                "title": str(safe_payload.get("title") or ""),
                "account_name": str(safe_payload.get("account_name") or ""),
                "author": str(safe_payload.get("author") or ""),
                "publish_time": str(safe_payload.get("publish_time") or ""),
                "url": clean_mp_url(str(safe_payload.get("url") or "")),
                "captured_at": str(safe_payload.get("captured_at") or utc_now()),
                "run_dir": str(run_dir),
                "snapshot_json": str(context["snapshot_json"]),
                "metrics_json": str(context["metrics_json"]),
                "report_md": str(context["report_md"]),
                "missing_metrics": [key for key, value in metrics.items() if value.get("source") == "missing"],
            }
            index_path = Path(context["index_path"])
            index_path.parent.mkdir(parents=True, exist_ok=True)
            with index_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(index_row, ensure_ascii=False, sort_keys=True) + "\n")
        if http:
            flow.response = http.Response.make(
                200,
                json.dumps({"ok": True, "snapshot_id": str(context.get("snapshot_id") or "")}, ensure_ascii=False),
                {"content-type": "application/json"},
            )

    def _capture_comments(self, flow: Any, query: dict, text: str, observe_path: Path) -> None:
        appmsgid = (query.get("appmsgid") or [""])[0]
        if not appmsgid:
            self.observe(flow, ["comment-no-appmsgid"], observe_path=observe_path)
            return
        try:
            payload = json.loads(text)
        except Exception:
            self.observe(flow, ["comment-non-json"], observe_path=observe_path)
            return
        if not isinstance(payload, dict):
            return
        comments = payload.get("elected_comment") or []
        if not isinstance(comments, list) or not comments:
            self.observe(flow, ["comment-empty"], 0, observe_path)
            return
        rows = []
        for c in comments:
            if not isinstance(c, dict):
                continue
            rows.append({
                "comment_id": str(c.get("id") or c.get("comment_id") or ""),
                "nick_name": str(c.get("nick_name") or c.get("username") or ""),
                "content": str(c.get("content") or ""),
                "like_count": c.get("like_num") or c.get("like_count") or 0,
                "create_time": c.get("create_time") or None,
            })
        if not rows:
            return
        capture_dir = self.base / "comments-capture"
        capture_dir.mkdir(parents=True, exist_ok=True)
        out = capture_dir / f"{appmsgid}.json"
        existing: list = []
        if out.exists():
            try:
                existing = json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        if not isinstance(existing, list):
            existing = []
        existing_ids = {r["comment_id"] for r in existing if r.get("comment_id")}
        new_rows = [r for r in rows if not r["comment_id"] or r["comment_id"] not in existing_ids]
        if not new_rows:
            self.observe(flow, ["comment-already-captured"], 0, observe_path)
            return
        merged = json.dumps(existing + new_rows, ensure_ascii=False, indent=2) + "\n"
        tmp = out.with_suffix(".tmp")
        tmp.write_text(merged, encoding="utf-8")
        os.replace(tmp, out)
        self.observe(flow, ["comment-captured"], len(new_rows), observe_path)


addons = [WeChatHistoryCapture()]
