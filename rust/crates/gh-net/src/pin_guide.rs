//! Bilingual `(lang_tag, pin_id)` `PinGuideFetcher` & `HoYoLABLabelCatalog` (`src/pin_guide.py`).

use crate::hd_assets::run_blocking_http;
use gh_core::{CoreError, CoreResult};
use reqwest::header::{ACCEPT, HeaderMap, HeaderValue, USER_AGENT};
use serde_json::Value;
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::io::Read;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

pub const API_BASE: &str = "https://sg-public-api.hoyolab.com/common/map_user/ys_obc";
pub const MAX_CACHE: usize = 64;
pub const MAX_CACHE_IMAGE_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_IMAGE_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_POINT_INFO_JSON_BYTES: usize = 512 * 1024;
pub const MAX_LABEL_TREE_JSON_BYTES: usize = 2 * 1024 * 1024;
pub const IMAGE_RESIZE_QS: &str = "x-oss-process=image/resize,w_960";
pub const LABEL_RETRY_BACKOFF_SECS: f64 = 60.0;
const WORKER_IDLE_TIMEOUT: Duration = Duration::from_secs(30);
const DEFAULT_USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GenshinHelper";

pub fn short_lang(lang: Option<&str>) -> &'static str {
    let normalized = lang.unwrap_or("vi").trim().to_ascii_lowercase();
    if normalized.starts_with("vi") {
        "vi"
    } else {
        "en"
    }
}

pub fn hoyolab_lang_tag(lang: Option<&str>) -> &'static str {
    if short_lang(lang) == "vi" {
        "vi-vn"
    } else {
        "en-us"
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PinGuide {
    pub pin_id: String,
    pub description: String,
    pub image_url: String,
    pub image_bytes: Option<Vec<u8>>,
    pub ok: bool,
    pub lang: String,
}

/// Strips HoYoLAB rich-text markup down to plain multi-line text.
pub fn clean_guide_text(raw: &str) -> String {
    if raw.is_empty() {
        return String::new();
    }

    // 1. Convert <br>, <br/>, <br /> and </p> into newlines, strip all other tags.
    let mut stripped = String::with_capacity(raw.len());
    let mut rest = raw;
    while let Some(lt) = rest.find('<') {
        stripped.push_str(&rest[..lt]);
        let after_lt = &rest[lt + 1..];
        if let Some(gt) = after_lt.find('>') {
            let tag_inner = &after_lt[..gt];
            if is_break_tag(tag_inner) {
                stripped.push('\n');
            }
            rest = &after_lt[gt + 1..];
        } else {
            stripped.push('<');
            stripped.push_str(after_lt);
            rest = "";
            break;
        }
    }
    stripped.push_str(rest);

    // 2. Unescape HTML entities.
    let unescaped = unescape_html(&stripped);

    // 3. Normalize line whitespace and drop blank lines.
    let normalized = unescaped.replace('\r', "");
    let lines: Vec<String> = normalized
        .split('\n')
        .map(|line| line.split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|line| !line.is_empty())
        .collect();
    lines.join("\n").trim().to_owned()
}

fn is_break_tag(tag_inner: &str) -> bool {
    let trimmed = tag_inner.trim();
    if let Some(after_slash) = trimmed.strip_prefix('/') {
        return after_slash.trim().eq_ignore_ascii_case("p");
    }
    let without_trailing_slash = trimmed.strip_suffix('/').unwrap_or(trimmed).trim();
    without_trailing_slash.eq_ignore_ascii_case("br")
}

fn unescape_html(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut rest = input;
    while let Some(amp) = rest.find('&') {
        out.push_str(&rest[..amp]);
        let after_amp = &rest[amp + 1..];
        if let Some(semi) = after_amp.find(';') {
            // Limit entity length so stray '&' doesn't swallow whole paragraphs.
            if semi > 0 && semi <= 12 {
                let entity = &after_amp[..semi];
                if let Some(decoded) = decode_html_entity(entity) {
                    out.push(decoded);
                    rest = &after_amp[semi + 1..];
                    continue;
                }
            }
        }
        out.push('&');
        rest = after_amp;
    }
    out.push_str(rest);
    out
}

fn decode_html_entity(entity: &str) -> Option<char> {
    if let Some(num) = entity.strip_prefix('#') {
        let code = if let Some(hex) = num.strip_prefix('x').or_else(|| num.strip_prefix('X')) {
            u32::from_str_radix(hex, 16).ok()?
        } else {
            num.parse::<u32>().ok()?
        };
        return char::from_u32(code);
    }

    match entity {
        "nbsp" => Some('\u{00A0}'),
        "amp" => Some('&'),
        "lt" => Some('<'),
        "gt" => Some('>'),
        "quot" => Some('"'),
        "apos" => Some('\''),
        "lsquo" => Some('\u{2018}'),
        "rsquo" => Some('\u{2019}'),
        "ldquo" => Some('\u{201C}'),
        "rdquo" => Some('\u{201D}'),
        "ndash" => Some('\u{2013}'),
        "mdash" => Some('\u{2014}'),
        "hellip" => Some('\u{2026}'),
        "bull" => Some('\u{2022}'),
        "times" => Some('\u{00D7}'),
        "deg" => Some('\u{00B0}'),
        "copy" => Some('\u{00A9}'),
        "reg" => Some('\u{00AE}'),
        "trade" => Some('\u{2122}'),
        _ => None,
    }
}

fn url_path_is_gif(url: &str) -> bool {
    let before_query_frag = url.split(['?', '#']).next().unwrap_or(url);
    let path = if let Some(scheme_pos) = before_query_frag.find("://") {
        let after_scheme = &before_query_frag[scheme_pos + 3..];
        match after_scheme.find('/') {
            Some(slash_pos) => &after_scheme[slash_pos..],
            None => "",
        }
    } else {
        before_query_frag
    };
    path.trim().to_ascii_lowercase().ends_with(".gif")
}

pub fn resized_image_url(url: &str) -> String {
    if url.is_empty() || url.contains("x-oss-process") {
        return url.to_owned();
    }
    let Some(scheme_pos) = url.find("://") else {
        return url.to_owned();
    };
    let after_scheme = &url[scheme_pos + 3..];
    let authority_end = after_scheme
        .find(['/', '?', '#'])
        .unwrap_or(after_scheme.len());
    let netloc = &after_scheme[..authority_end];
    let host = netloc
        .split(':')
        .next()
        .unwrap_or(netloc)
        .to_ascii_lowercase();
    let allowed = ["hoyoverse.com", "hoyolab.com", "mihoyo.com"]
        .iter()
        .any(|d| host == *d || host.ends_with(&format!(".{d}")));
    if !allowed {
        return url.to_owned();
    }
    let has_query = after_scheme
        .split('#')
        .next()
        .is_some_and(|before_frag| before_frag.contains('?'));
    let sep = if has_query { '&' } else { '?' };
    format!("{url}{sep}{IMAGE_RESIZE_QS}")
}

pub type JsonFetchHook = Arc<dyn Fn(&str) -> Result<Value, String> + Send + Sync>;
pub type BytesFetchHook = Arc<dyn Fn(&str) -> Result<Vec<u8>, String> + Send + Sync>;

pub struct PinGuideConfig {
    pub lang_getter: Arc<dyn Fn() -> String + Send + Sync>,
    pub api_base: String,
    pub fetch_json: Option<JsonFetchHook>,
    pub fetch_bytes: Option<BytesFetchHook>,
    pub timeout: Duration,
}

impl Default for PinGuideConfig {
    fn default() -> Self {
        Self {
            lang_getter: Arc::new(|| "vi".to_owned()),
            api_base: API_BASE.to_owned(),
            fetch_json: None,
            fetch_bytes: None,
            timeout: Duration::from_secs(8),
        }
    }
}

type PendingJob = (String, String, i32, Box<dyn FnOnce(PinGuide) + Send>);

#[derive(Default)]
struct PinGuideState {
    cache: VecDeque<((String, String), PinGuide)>,
    cache_image_bytes: usize,
    pending: Option<PendingJob>,
    inflight: Option<(String, String)>,
    thread_alive: bool,
    stopped: bool,
}

#[inline]
fn guide_image_bytes(guide: &PinGuide) -> usize {
    guide.image_bytes.as_ref().map_or(0, Vec::len)
}

struct PinGuideInner {
    lang_getter: Arc<dyn Fn() -> String + Send + Sync>,
    api_base: String,
    fetch_json: Option<JsonFetchHook>,
    fetch_bytes: Option<BytesFetchHook>,
    timeout: Duration,
    state: Mutex<PinGuideState>,
    cv: Condvar,
}

#[derive(Clone)]
pub struct PinGuideFetcher {
    inner: Arc<PinGuideInner>,
}

impl std::fmt::Debug for PinGuideFetcher {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PinGuideFetcher")
            .field("api_base", &self.inner.api_base)
            .field("timeout", &self.inner.timeout)
            .finish()
    }
}

impl Default for PinGuideFetcher {
    fn default() -> Self {
        Self::with_config(PinGuideConfig::default())
    }
}

impl PinGuideFetcher {
    pub fn new(lang_getter: Arc<dyn Fn() -> String + Send + Sync>) -> Self {
        Self::with_config(PinGuideConfig {
            lang_getter,
            ..Default::default()
        })
    }

    pub fn with_config(cfg: PinGuideConfig) -> Self {
        let trimmed = cfg.api_base.trim().trim_end_matches('/').to_owned();
        Self {
            inner: Arc::new(PinGuideInner {
                lang_getter: cfg.lang_getter,
                api_base: if trimmed.is_empty() {
                    API_BASE.to_owned()
                } else {
                    trimmed
                },
                fetch_json: cfg.fetch_json,
                fetch_bytes: cfg.fetch_bytes,
                timeout: cfg.timeout.max(Duration::from_millis(50)),
                state: Mutex::new(PinGuideState::default()),
                cv: Condvar::new(),
            }),
        }
    }

    pub fn is_fetchable_id(pin_id: &str) -> bool {
        let trimmed = pin_id.trim();
        !trimmed.is_empty() && trimmed.bytes().all(|b| b.is_ascii_digit())
    }

    fn active_lang_tag(&self, lang: Option<&str>) -> &'static str {
        match lang {
            Some(l) => hoyolab_lang_tag(Some(l)),
            None => {
                let current = (self.inner.lang_getter)();
                hoyolab_lang_tag(Some(&current))
            }
        }
    }

    pub fn get_cached(&self, pin_id: &str, lang: Option<&str>) -> Option<PinGuide> {
        let key = (self.active_lang_tag(lang).to_owned(), pin_id.to_owned());
        let mut st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
        let pos = st.cache.iter().position(|(k, _)| k == &key)?;
        let entry = st.cache.remove(pos)?;
        let out = entry.1.clone();
        st.cache.push_back(entry);
        Some(out)
    }

    pub fn is_loading(&self, pin_id: &str, lang: Option<&str>) -> bool {
        let key = (self.active_lang_tag(lang).to_owned(), pin_id.to_owned());
        let st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
        st.inflight.as_ref() == Some(&key)
            || st
                .pending
                .as_ref()
                .is_some_and(|(lt, pid, _, _)| lt == &key.0 && pid == &key.1)
    }

    pub fn request<F>(&self, pin_id: &str, map_id: i32, on_done: F, lang: Option<&str>) -> bool
    where
        F: FnOnce(PinGuide) + Send + 'static,
    {
        let pid = pin_id.trim().to_owned();
        if !Self::is_fetchable_id(&pid) {
            return false;
        }
        let lang_tag = self.active_lang_tag(lang).to_owned();
        let cache_key = (lang_tag.clone(), pid.clone());

        let mut st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
        if st.stopped {
            return false;
        }
        if st.cache.iter().any(|(k, _)| k == &cache_key) {
            return false;
        }
        if st.inflight.as_ref() == Some(&cache_key) {
            return false;
        }

        st.pending = Some((lang_tag, pid, map_id, Box::new(on_done)));
        if !st.thread_alive {
            st.thread_alive = true;
            let inner = Arc::clone(&self.inner);
            if std::thread::Builder::new()
                .name("GH_PinGuide".to_owned())
                .spawn(move || worker_loop(inner))
                .is_err()
            {
                st.thread_alive = false;
                st.pending = None;
                return false;
            }
        }
        self.inner.cv.notify_one();
        true
    }

    pub fn stop(&self) {
        let mut st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
        st.stopped = true;
        st.pending = None;
        self.inner.cv.notify_all();
    }

    pub fn fetch_sync(&self, pin_id: &str, map_id: i32, lang: Option<&str>) -> PinGuide {
        let lang_tag = self.active_lang_tag(lang).to_owned();
        fetch_sync_inner(&self.inner, pin_id, map_id, &lang_tag)
    }
}

impl Drop for PinGuideFetcher {
    fn drop(&mut self) {
        if Arc::strong_count(&self.inner) == 1 {
            self.stop();
        }
    }
}

fn fetch_sync_inner(
    inner: &PinGuideInner,
    pin_id: &str,
    map_id: i32,
    lang_tag: &str,
) -> PinGuide {
    let encoded_pid = url_encode_component(pin_id);
    let url = format!(
        "{}/v1/map/point/info?map_id={map_id}&point_id={encoded_pid}&app_sn=ys_obc&lang={lang_tag}",
        inner.api_base
    );

    let json_res = match &inner.fetch_json {
        Some(hook) => hook(&url),
        None => http_get_json(&url, inner.timeout, MAX_POINT_INFO_JSON_BYTES),
    };

    let Ok(env) = json_res else {
        return PinGuide {
            pin_id: pin_id.to_owned(),
            description: String::new(),
            image_url: String::new(),
            image_bytes: None,
            ok: false,
            lang: lang_tag.to_owned(),
        };
    };

    if !is_retcode_ok(&env) {
        return PinGuide {
            pin_id: pin_id.to_owned(),
            description: String::new(),
            image_url: String::new(),
            image_bytes: None,
            ok: false,
            lang: lang_tag.to_owned(),
        };
    }

    let info = env.get("data").and_then(|d| d.get("info"));
    let raw_content = info
        .and_then(|i| i.get("content"))
        .and_then(Value::as_str)
        .unwrap_or_default();
    let desc = clean_guide_text(raw_content);

    let mut img_url = info
        .and_then(|i| i.get("img"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_owned();
    if img_url.is_empty()
        && let Some(first) = info
            .and_then(|i| i.get("url_list"))
            .and_then(Value::as_array)
            .and_then(|arr| arr.first())
    {
        if let Some(s) = first.as_str() {
            img_url = s.trim().to_owned();
        } else if let Some(s) = first.get("url").and_then(Value::as_str) {
            img_url = s.trim().to_owned();
        }
    }

    let mut image_bytes = None;
    let allow_img = img_url.starts_with("https://")
        || (inner.api_base.starts_with("http://") && img_url.starts_with("http://"));
    if allow_img {
        let resized = resized_image_url(&img_url);
        let img_total_timeout = inner
            .timeout
            .saturating_mul(3)
            .min(Duration::from_secs(25))
            .max(inner.timeout);
        let resized_timeout = if url_path_is_gif(&img_url) {
            inner.timeout
        } else {
            img_total_timeout
        };
        let fetch_img = |target: &str, total_timeout: Duration| -> Result<Vec<u8>, String> {
            let bytes = match &inner.fetch_bytes {
                Some(hook) => hook(target)?,
                None => http_get_bytes_with_timeouts(
                    target,
                    inner.timeout,
                    total_timeout,
                    MAX_IMAGE_BYTES,
                )?,
            };
            if bytes.is_empty() {
                return Err("empty image response".to_owned());
            }
            Ok(bytes)
        };
        if let Ok(bytes) = fetch_img(&resized, resized_timeout) {
            image_bytes = Some(bytes);
        } else if resized != img_url
            && let Ok(bytes) = fetch_img(&img_url, img_total_timeout)
        {
            image_bytes = Some(bytes);
        }
    }

    PinGuide {
        pin_id: pin_id.to_owned(),
        description: desc,
        image_url: img_url,
        image_bytes,
        ok: true,
        lang: lang_tag.to_owned(),
    }
}

fn worker_loop(inner: Arc<PinGuideInner>) {
    loop {
        let (lang_tag, pid, map_id, cb) = {
            let mut st = inner.state.lock().unwrap_or_else(|e| e.into_inner());
            while st.pending.is_none() && !st.stopped {
                let (guard, _wait_res) = inner
                    .cv
                    .wait_timeout(st, WORKER_IDLE_TIMEOUT)
                    .unwrap_or_else(|e| e.into_inner());
                st = guard;
                if st.pending.is_none() && !st.stopped {
                    st.thread_alive = false;
                    return;
                }
            }
            if st.stopped {
                st.thread_alive = false;
                return;
            }
            let Some((lt, p, mid, callback)) = st.pending.take() else {
                continue;
            };
            st.inflight = Some((lt.clone(), p.clone()));
            (lt, p, mid, callback)
        };

        let guide = fetch_sync_inner(&inner, &pid, map_id, &lang_tag);
        let cache_key = (lang_tag, pid);

        let should_cache = guide.ok
            && (guide.image_url.is_empty()
                || guide.image_bytes.as_ref().is_some_and(|b| !b.is_empty()));

        {
            let mut st = inner.state.lock().unwrap_or_else(|e| e.into_inner());
            st.inflight = None;
            if should_cache {
                if let Some(pos) = st.cache.iter().position(|(k, _)| k == &cache_key)
                    && let Some((_, old)) = st.cache.remove(pos)
                {
                    st.cache_image_bytes = st
                        .cache_image_bytes
                        .saturating_sub(guide_image_bytes(&old));
                }
                st.cache_image_bytes = st
                    .cache_image_bytes
                    .saturating_add(guide_image_bytes(&guide));
                st.cache.push_back((cache_key, guide.clone()));
                while st.cache.len() > MAX_CACHE
                    || (st.cache_image_bytes > MAX_CACHE_IMAGE_BYTES && st.cache.len() > 1)
                {
                    if let Some((_, evicted)) = st.cache.pop_front() {
                        st.cache_image_bytes = st
                            .cache_image_bytes
                            .saturating_sub(guide_image_bytes(&evicted));
                    } else {
                        break;
                    }
                }
            }
        }

        cb(guide);
    }
}

pub type OnLabelsLoadedHook = Arc<dyn Fn(i32, &str) + Send + Sync>;

pub struct LabelCatalogConfig {
    pub api_base: String,
    pub fetch_json: Option<JsonFetchHook>,
    pub on_loaded: Option<OnLabelsLoadedHook>,
    pub timeout: Duration,
}

impl Default for LabelCatalogConfig {
    fn default() -> Self {
        Self {
            api_base: API_BASE.to_owned(),
            fetch_json: None,
            on_loaded: None,
            timeout: Duration::from_secs(8),
        }
    }
}

const ALLOWED_ICON_HOSTS: &[&str] = &[
    "act-webstatic.hoyoverse.com",
    "upload-os-bbs.hoyolab.com",
    "webstatic.hoyoverse.com",
    "fastcdn.hoyoverse.com",
    "act.hoyolab.com",
];

fn allowlisted_icon_url(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    let rest = trimmed.strip_prefix("https://")?;
    let host_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let host_raw = &rest[..host_end];
    if host_raw.is_empty()
        || host_raw.contains('@')
        || host_raw.contains(':')
        || trimmed.chars().any(char::is_control)
    {
        return None;
    }
    let host = host_raw.to_ascii_lowercase();
    if ALLOWED_ICON_HOSTS.contains(&host.as_str()) {
        Some(trimmed.to_owned())
    } else {
        None
    }
}

#[derive(Default)]
struct LabelCatalogState {
    labels: HashMap<(i32, String), BTreeMap<i32, String>>,
    icons: HashMap<i32, BTreeMap<i32, String>>,
    inflight: HashSet<(i32, String)>,
    retry_after: HashMap<(i32, String), Instant>,
}

struct LabelCatalogInner {
    api_base: String,
    fetch_json: Option<JsonFetchHook>,
    on_loaded: Option<OnLabelsLoadedHook>,
    timeout: Duration,
    state: Mutex<LabelCatalogState>,
}

#[derive(Clone)]
pub struct HoYoLABLabelCatalog {
    inner: Arc<LabelCatalogInner>,
}

impl std::fmt::Debug for HoYoLABLabelCatalog {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HoYoLABLabelCatalog")
            .field("api_base", &self.inner.api_base)
            .finish()
    }
}

impl Default for HoYoLABLabelCatalog {
    fn default() -> Self {
        Self::with_config(LabelCatalogConfig::default())
    }
}

impl HoYoLABLabelCatalog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_config(cfg: LabelCatalogConfig) -> Self {
        let trimmed = cfg.api_base.trim().trim_end_matches('/').to_owned();
        Self {
            inner: Arc::new(LabelCatalogInner {
                api_base: if trimmed.is_empty() {
                    API_BASE.to_owned()
                } else {
                    trimmed
                },
                fetch_json: cfg.fetch_json,
                on_loaded: cfg.on_loaded,
                timeout: cfg.timeout.max(Duration::from_millis(50)),
                state: Mutex::new(LabelCatalogState::default()),
            }),
        }
    }

    pub fn get_map_name(map_id: i32, lang: Option<&str>, fallback: &str) -> String {
        let short = short_lang(lang);
        let pair: Option<(&str, &str)> = match map_id {
            2 => Some(("Teyvat Interactive Map", "Bản Đồ Tương Tác Teyvat")),
            7 => Some(("Enkanomiya", "Enkanomiya")),
            9 => Some((
                "The Chasm: Underground Mines",
                "Vực Đá Sâu - Khu Mỏ Ngầm",
            )),
            34 => Some(("Sea of Bygone Eras", "Biển Kỷ Nguyên Cũ")),
            36 => Some(("Ancient Sacred Mountain", "Núi Thánh Cổ Đại")),
            37 => Some(("Temple of Space", "Đền Thờ Không Gian")),
            40 => Some(("Nod-Krai", "Nod-Krai")),
            _ => None,
        };

        if let Some((en, vi)) = pair {
            let localized = if short == "vi" { vi } else { en };
            if !localized.is_empty() {
                return localized.to_owned();
            }
            if !en.is_empty() {
                return en.to_owned();
            }
        }
        if !fallback.is_empty() {
            fallback.to_owned()
        } else {
            format!("Map {map_id}")
        }
    }

    pub fn fetch_sync(&self, map_id: i32, lang: Option<&str>) -> CoreResult<BTreeMap<i32, String>> {
        let short = short_lang(lang).to_owned();
        let lang_tag = hoyolab_lang_tag(Some(&short));
        let url = format!(
            "{}/v1/map/label/tree?map_id={map_id}&app_sn=ys_obc&lang={lang_tag}",
            self.inner.api_base
        );

        let env = match &self.inner.fetch_json {
            Some(hook) => hook(&url).map_err(CoreError::Sync)?,
            None => http_get_json(&url, self.inner.timeout, MAX_LABEL_TREE_JSON_BYTES)
                .map_err(CoreError::Sync)?,
        };

        if !is_retcode_ok(&env) {
            return Err(CoreError::Sync(format!(
                "HoYoLAB label/tree error retcode={:?}",
                env.get("retcode")
            )));
        }

        let mut parsed = BTreeMap::new();
        let mut parsed_icons = BTreeMap::new();
        if let Some(tree) = env
            .get("data")
            .and_then(|d| d.get("tree"))
            .and_then(Value::as_array)
        {
            walk_label_tree(tree, &mut parsed);
            walk_label_icons(tree, &mut parsed_icons);
        }

        let mut st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
        let key = (map_id, short);
        st.labels.insert(key.clone(), parsed.clone());
        let icon_entry = st.icons.entry(map_id).or_default();
        for (lid, icon_url) in parsed_icons {
            icon_entry.insert(lid, icon_url);
        }
        st.retry_after.remove(&key);
        Ok(parsed)
    }

    pub fn prefetch(&self, map_id: i32, lang: Option<&str>) {
        let short = short_lang(lang).to_owned();
        let key = (map_id, short.clone());
        {
            let mut st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
            if st.labels.contains_key(&key) || st.inflight.contains(&key) {
                return;
            }
            if let Some(until) = st.retry_after.get(&key)
                && Instant::now() < *until
            {
                return;
            }
            st.inflight.insert(key.clone());
        }

        let catalog = self.clone();
        let _ = std::thread::Builder::new()
            .name(format!("GH_Labels_{map_id}_{short}"))
            .spawn(move || {
                let ok = match catalog.fetch_sync(map_id, Some(&short)) {
                    Ok(_) => true,
                    Err(_) => {
                        let mut st = catalog.inner.state.lock().unwrap_or_else(|e| e.into_inner());
                        st.retry_after.insert(
                            key.clone(),
                            Instant::now() + Duration::from_secs_f64(LABEL_RETRY_BACKOFF_SECS),
                        );
                        false
                    }
                };
                {
                    let mut st = catalog.inner.state.lock().unwrap_or_else(|e| e.into_inner());
                    st.inflight.remove(&key);
                }
                if ok && let Some(cb) = &catalog.inner.on_loaded {
                    cb(map_id, &short);
                }
            });
    }

    pub fn get_label(
        &self,
        map_id: i32,
        label_id: Option<i32>,
        lang: Option<&str>,
    ) -> Option<String> {
        let lid = label_id?;
        let short = short_lang(lang);
        let cached = {
            let st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
            st.labels
                .get(&(map_id, short.to_owned()))
                .and_then(|m| m.get(&lid).cloned())
        };
        if let Some(name) = cached {
            return Some(name);
        }
        let is_loaded = {
            let st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
            st.labels.contains_key(&(map_id, short.to_owned()))
        };
        if !is_loaded {
            self.prefetch(map_id, Some(short));
        }
        None
    }

    pub fn get_icon_url(&self, map_id: i32, label_id: i32) -> Option<String> {
        let cached = {
            let st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
            st.icons
                .get(&map_id)
                .and_then(|m| m.get(&label_id).cloned())
        };
        if let Some(url) = cached {
            return Some(url);
        }
        let is_loaded = {
            let st = self.inner.state.lock().unwrap_or_else(|e| e.into_inner());
            st.icons.contains_key(&map_id)
        };
        if !is_loaded {
            self.prefetch(map_id, None);
        }
        None
    }
}

fn walk_label_tree(nodes: &[Value], out: &mut BTreeMap<i32, String>) {
    for item in nodes {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let raw_id = obj.get("id").and_then(|v| match v {
            Value::Number(n) => n.as_i64().and_then(|i| i32::try_from(i).ok()),
            Value::String(s) => s.trim().parse::<i32>().ok(),
            _ => None,
        });
        let raw_name = obj
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim();
        if let Some(lid) = raw_id
            && !raw_name.is_empty()
        {
            out.insert(lid, raw_name.to_owned());
        }
        if let Some(children) = obj.get("children").and_then(Value::as_array)
            && !children.is_empty()
        {
            walk_label_tree(children, out);
        }
    }
}

fn walk_label_icons(nodes: &[Value], out: &mut BTreeMap<i32, String>) {
    for item in nodes {
        let Some(obj) = item.as_object() else {
            continue;
        };
        let raw_id = obj.get("id").and_then(|v| match v {
            Value::Number(n) => n.as_i64().and_then(|i| i32::try_from(i).ok()),
            Value::String(s) => s.trim().parse::<i32>().ok(),
            _ => None,
        });
        if let Some(lid) = raw_id
            && let Some(raw_icon) = obj.get("icon").and_then(Value::as_str)
            && let Some(safe_url) = allowlisted_icon_url(raw_icon)
        {
            out.insert(lid, safe_url);
        }
        if let Some(children) = obj.get("children").and_then(Value::as_array)
            && !children.is_empty()
        {
            walk_label_icons(children, out);
        }
    }
}

fn is_retcode_ok(env: &Value) -> bool {
    match env.get("retcode") {
        Some(Value::Number(n)) => n.as_i64() == Some(0),
        Some(Value::String(s)) => s.trim() == "0",
        _ => false,
    }
}

fn url_encode_component(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for &b in raw.as_bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

fn http_get_bytes(url: &str, timeout: Duration, limit: usize) -> Result<Vec<u8>, String> {
    http_get_bytes_with_timeouts(url, timeout, timeout, limit)
}

fn http_get_bytes_with_timeouts(
    url: &str,
    connect_timeout: Duration,
    total_timeout: Duration,
    limit: usize,
) -> Result<Vec<u8>, String> {
    let url_owned = url.to_owned();
    let res = run_blocking_http(move || {
        let mut headers = HeaderMap::new();
        headers.insert(USER_AGENT, HeaderValue::from_static(DEFAULT_USER_AGENT));
        headers.insert(ACCEPT, HeaderValue::from_static("application/json, image/*"));
        let client = reqwest::blocking::Client::builder()
            .connect_timeout(connect_timeout)
            .timeout(total_timeout)
            .build()
            .ok()?;
        let resp = client.get(&url_owned).headers(headers).send().ok()?;
        if !resp.status().is_success() {
            return Some(Err(format!("HTTP {}", resp.status())));
        }
        let mut buf = Vec::new();
        let mut limited = resp.take((limit + 1) as u64);
        if let Err(e) = limited.read_to_end(&mut buf) {
            return Some(Err(format!("read failed: {e}")));
        }
        if buf.len() > limit {
            return Some(Err("response too large".to_owned()));
        }
        Some(Ok(buf))
    });
    match res {
        Some(Ok(bytes)) => Ok(bytes),
        Some(Err(e)) => Err(e),
        None => Err("HTTP request failed".to_owned()),
    }
}

fn http_get_json(url: &str, timeout: Duration, limit: usize) -> Result<Value, String> {
    let bytes = http_get_bytes(url, timeout, limit)?;
    serde_json::from_slice::<Value>(&bytes).map_err(|e| format!("invalid JSON: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::Router;
    use axum::extract::Query;
    use axum::response::IntoResponse;
    use axum::routing::get;
    use serde_json::json;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::net::TcpListener;

    #[test]
    fn clean_guide_text_and_resized_image_url_match_python() {
        assert_eq!(
            clean_guide_text("Đi <b>qua</b> cầu<br/>rẽ trái&nbsp;sau đá"),
            "Đi qua cầu\nrẽ trái sau đá"
        );
        assert_eq!(
            clean_guide_text("<p>Sau thác nước</p><p>Nhặt rương&amp;mở khóa&#33;</p>"),
            "Sau thác nước\nNhặt rương&mở khóa!"
        );
        assert_eq!(
            clean_guide_text("  &#x41; &lt;tag&gt; &quot;ok&quot; &apos;hi&apos;\r\n\r\n  line2  "),
            "A <tag> \"ok\" 'hi'\nline2"
        );
        assert_eq!(clean_guide_text(""), "");

        assert_eq!(
            resized_image_url("https://upload-os-bbs.hoyolab.com/a.png"),
            "https://upload-os-bbs.hoyolab.com/a.png?x-oss-process=image/resize,w_960"
        );
        assert_eq!(
            resized_image_url("https://act-webstatic.hoyoverse.com/a.png?v=1"),
            "https://act-webstatic.hoyoverse.com/a.png?v=1&x-oss-process=image/resize,w_960"
        );
        assert_eq!(
            resized_image_url("https://cdn.mihoyo.com/a.png?x-oss-process=image/resize,w_480"),
            "https://cdn.mihoyo.com/a.png?x-oss-process=image/resize,w_480"
        );
        assert_eq!(
            resized_image_url("https://example.com/a.png"),
            "https://example.com/a.png"
        );
    }

    #[test]
    fn verify_nav_ux_v54_guide_fetcher_contract() {
        let seen_urls = Arc::new(Mutex::new(Vec::<String>::new()));
        let seen_urls_json = Arc::clone(&seen_urls);
        let seen_img_urls = Arc::new(Mutex::new(Vec::<String>::new()));
        let seen_img_urls_hook = Arc::clone(&seen_img_urls);

        let fetcher = PinGuideFetcher::with_config(PinGuideConfig {
            lang_getter: Arc::new(|| "vi".to_owned()),
            fetch_json: Some(Arc::new(move |url| {
                seen_urls_json.lock().unwrap().push(url.to_owned());
                Ok(json!({
                    "retcode": 0,
                    "data": {
                        "info": {
                            "content": "<p>Sau thác nước</p>",
                            "img": "https://upload-os-bbs.hoyoverse.com/guide.png"
                        }
                    }
                }))
            })),
            fetch_bytes: Some(Arc::new(move |url| {
                seen_img_urls_hook.lock().unwrap().push(url.to_owned());
                Ok(b"PNG_DATA".to_vec())
            })),
            ..Default::default()
        });

        // Rejects non-digit IDs
        assert!(!PinGuideFetcher::is_fetchable_id("pin_1.0_2.0"));
        assert!(!fetcher.request("pin_1.0_2.0", 2, |_| {}, None));

        // Async request + wait
        let (tx, rx) = std::sync::mpsc::channel();
        assert!(fetcher.request(
            "107596",
            2,
            move |g| {
                let _ = tx.send(g);
            },
            None
        ));
        let guide = rx.recv_timeout(Duration::from_secs(3)).expect("guide");
        assert!(guide.ok);
        assert_eq!(guide.pin_id, "107596");
        assert_eq!(guide.description, "Sau thác nước");
        assert_eq!(guide.image_bytes.as_deref(), Some(b"PNG_DATA".as_slice()));
        assert_eq!(guide.lang, "vi-vn");

        let api_url = &seen_urls.lock().unwrap()[0];
        assert!(api_url.contains("point_id=107596"));
        assert!(api_url.contains("lang=vi-vn"));
        assert!(seen_img_urls.lock().unwrap()[0].contains("x-oss-process=image/resize,w_960"));

        // Second request for same (lang_tag, pin_id) hits cache -> returns false
        assert!(!fetcher.request("107596", 2, |_| {}, None));
        assert!(fetcher.get_cached("107596", None).is_some());
    }

    #[test]
    fn bilingual_cache_isolation_and_label_catalog() {
        let active_lang = Arc::new(Mutex::new("vi".to_owned()));
        let active_lang_getter = Arc::clone(&active_lang);

        let fetcher = PinGuideFetcher::with_config(PinGuideConfig {
            lang_getter: Arc::new(move || active_lang_getter.lock().unwrap().clone()),
            fetch_json: Some(Arc::new(|url| {
                let is_en = url.contains("lang=en-us");
                let text = if is_en {
                    "English community tip"
                } else {
                    "Gợi ý tiếng Việt"
                };
                Ok(json!({
                    "retcode": 0,
                    "data": { "info": { "content": text, "img": "" } }
                }))
            })),
            fetch_bytes: Some(Arc::new(|_| Ok(Vec::new()))),
            ..Default::default()
        });

        let (tx1, rx1) = std::sync::mpsc::channel();
        assert!(fetcher.request(
            "12345",
            2,
            move |g| {
                let _ = tx1.send(g);
            },
            None
        ));
        let g_vi = rx1.recv_timeout(Duration::from_secs(3)).unwrap();
        assert_eq!(g_vi.description, "Gợi ý tiếng Việt");
        assert_eq!(g_vi.lang, "vi-vn");

        // Switch active language to "en" -> cached VI entry must NOT satisfy EN lookup
        *active_lang.lock().unwrap() = "en".to_owned();
        assert!(fetcher.get_cached("12345", None).is_none());

        let (tx2, rx2) = std::sync::mpsc::channel();
        assert!(fetcher.request(
            "12345",
            2,
            move |g| {
                let _ = tx2.send(g);
            },
            None
        ));
        let g_en = rx2.recv_timeout(Duration::from_secs(3)).unwrap();
        assert_eq!(g_en.description, "English community tip");
        assert_eq!(g_en.lang, "en-us");

        // Both remain independently cached
        assert_eq!(
            fetcher.get_cached("12345", Some("vi")).unwrap().description,
            "Gợi ý tiếng Việt"
        );
        assert_eq!(
            fetcher.get_cached("12345", Some("en")).unwrap().description,
            "English community tip"
        );

        // HoYoLABLabelCatalog bilingual lookup + map names
        let loaded_events = Arc::new(Mutex::new(Vec::<(i32, String)>::new()));
        let loaded_hook = Arc::clone(&loaded_events);
        let catalog = HoYoLABLabelCatalog::with_config(LabelCatalogConfig {
            fetch_json: Some(Arc::new(|url| {
                let is_vi = url.contains("lang=vi-vn");
                let name18 = if is_vi {
                    "Khiêu Chiến Hạn Giờ"
                } else {
                    "Time Trial Challenge"
                };
                Ok(json!({
                    "retcode": 0,
                    "data": {
                        "tree": [{
                            "id": 1,
                            "name": "Root",
                            "children": [{ "id": 18, "name": name18 }]
                        }]
                    }
                }))
            })),
            on_loaded: Some(Arc::new(move |mid, short| {
                loaded_hook.lock().unwrap().push((mid, short.to_owned()));
            })),
            ..Default::default()
        });

        catalog.fetch_sync(2, Some("vi")).unwrap();
        catalog.fetch_sync(2, Some("en")).unwrap();
        assert_eq!(
            catalog.get_label(2, Some(18), Some("vi")).as_deref(),
            Some("Khiêu Chiến Hạn Giờ")
        );
        assert_eq!(
            catalog.get_label(2, Some(18), Some("en")).as_deref(),
            Some("Time Trial Challenge")
        );
        assert_eq!(
            HoYoLABLabelCatalog::get_map_name(2, Some("en"), ""),
            "Teyvat Interactive Map"
        );
        assert_eq!(
            HoYoLABLabelCatalog::get_map_name(2, Some("vi"), ""),
            "Bản Đồ Tương Tác Teyvat"
        );
        assert_eq!(
            HoYoLABLabelCatalog::get_map_name(999, Some("en"), "Fallback"),
            "Fallback"
        );
        assert_eq!(
            HoYoLABLabelCatalog::get_map_name(999, Some("en"), ""),
            "Map 999"
        );

        // Async prefetch triggers on_loaded callback
        catalog.prefetch(7, Some("en"));
        for _ in 0..40 {
            if !loaded_events.lock().unwrap().is_empty() {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(
            loaded_events
                .lock()
                .unwrap()
                .contains(&(7, "en".to_owned()))
        );
    }

    #[test]
    fn lru_eviction_at_max_cache_64() {
        let fetcher = PinGuideFetcher::with_config(PinGuideConfig {
            fetch_json: Some(Arc::new(|url| {
                Ok(json!({
                    "retcode": 0,
                    "data": { "info": { "content": format!("tip:{url}"), "img": "" } }
                }))
            })),
            ..Default::default()
        });

        for i in 1..=65u32 {
            let (tx, rx) = std::sync::mpsc::channel();
            assert!(fetcher.request(
                &i.to_string(),
                2,
                move |g| {
                    let _ = tx.send(g);
                },
                Some("vi")
            ));
            let g = rx.recv_timeout(Duration::from_secs(3)).unwrap();
            assert!(g.ok);
        }

        // Pin "1" was oldest and should be evicted, while "2".."65" remain cached
        assert!(fetcher.get_cached("1", Some("vi")).is_none());
        assert!(fetcher.get_cached("2", Some("vi")).is_some());
        assert!(fetcher.get_cached("65", Some("vi")).is_some());
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn real_http_pin_guide_and_label_catalog_inside_tokio() {
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_srv = Arc::clone(&calls);
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let app = Router::new()
            .route(
                "/v1/map/point/info",
                get(move |Query(q): Query<HashMap<String, String>>| {
                    let c = Arc::clone(&calls_srv);
                    async move {
                        c.fetch_add(1, Ordering::SeqCst);
                        let pid = q.get("point_id").cloned().unwrap_or_default();
                        axum::Json(json!({
                            "retcode": 0,
                            "data": {
                                "info": {
                                    "content": format!("Pin <b>{pid}</b><br/>ready"),
                                    "url_list": [{ "url": format!("http://{addr}/img.png") }]
                                }
                            }
                        }))
                    }
                }),
            )
            .route(
                "/v1/map/label/tree",
                get(|| async {
                    axum::Json(json!({
                        "retcode": "0",
                        "data": { "tree": [{ "id": 1, "name": "Cat", "children": [{ "id": 2, "name": "Teleport" }] }] }
                    }))
                }),
            )
            .route(
                "/img.png",
                get(|| async { ([(axum::http::header::CONTENT_TYPE, "image/png")], b"FAKEPNG".to_vec()).into_response() }),
            );

        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        tokio::spawn(async move {
            let _ = axum::serve(listener, app)
                .with_graceful_shutdown(async {
                    let _ = rx.await;
                })
                .await;
        });

        let base = format!("http://{addr}");
        let fetcher = PinGuideFetcher::with_config(PinGuideConfig {
            api_base: base.clone(),
            ..Default::default()
        });
        let guide = fetcher.fetch_sync("42", 2, Some("en"));
        assert!(guide.ok);
        assert_eq!(guide.description, "Pin 42\nready");
        assert_eq!(guide.image_bytes.as_deref(), Some(b"FAKEPNG".as_slice()));
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        let catalog = HoYoLABLabelCatalog::with_config(LabelCatalogConfig {
            api_base: base,
            ..Default::default()
        });
        let labels = catalog.fetch_sync(2, Some("en")).unwrap();
        assert_eq!(labels.get(&2).map(String::as_str), Some("Teleport"));

        let _ = tx.send(());
    }

    #[test]
    fn label_catalog_extracts_allowlisted_icons_and_rejects_untrusted_hosts() {
        let catalog = HoYoLABLabelCatalog::with_config(LabelCatalogConfig {
            fetch_json: Some(Arc::new(|_| {
                Ok(json!({
                    "retcode": 0,
                    "data": {
                        "tree": [{
                            "id": 1,
                            "name": "Teleports",
                            "icon": "https://act-webstatic.hoyoverse.com/map_manage/tp.png",
                            "children": [
                                {
                                    "id": 2,
                                    "name": "Statue",
                                    "icon": "https://upload-os-bbs.hoyolab.com/statue.webp?v=2"
                                },
                                {
                                    "id": 3,
                                    "name": "UntrustedHttp",
                                    "icon": "http://act-webstatic.hoyoverse.com/bad.png"
                                },
                                {
                                    "id": 4,
                                    "name": "UntrustedHost",
                                    "icon": "https://evil.example.com/pwn.png"
                                },
                                {
                                    "id": 5,
                                    "name": "UserInfoBypass",
                                    "icon": "https://act.hoyolab.com@evil.example.com/pwn.png"
                                }
                            ]
                        }]
                    }
                }))
            })),
            ..Default::default()
        });

        catalog.fetch_sync(2, Some("vi")).unwrap();
        assert_eq!(
            catalog.get_icon_url(2, 1).as_deref(),
            Some("https://act-webstatic.hoyoverse.com/map_manage/tp.png")
        );
        assert_eq!(
            catalog.get_icon_url(2, 2).as_deref(),
            Some("https://upload-os-bbs.hoyolab.com/statue.webp?v=2")
        );
        assert!(catalog.get_icon_url(2, 3).is_none());
        assert!(catalog.get_icon_url(2, 4).is_none());
        assert!(catalog.get_icon_url(2, 5).is_none());
    }

    #[test]
    fn gif_fallback_on_empty_or_failed_resize_and_retry_when_image_fails() {
        assert_eq!(MAX_IMAGE_BYTES, 16 * 1024 * 1024);
        assert!(url_path_is_gif("https://upload-os-bbs.hoyolab.com/clip.GIF?v=1#top"));
        assert!(!url_path_is_gif("https://example.gif/clip.png"));
        assert!(!url_path_is_gif("https://example.gif"));

        // 1. Resized OSS URL returning empty body or error falls back to raw GIF URL.
        let calls = Arc::new(Mutex::new(Vec::<String>::new()));
        let calls_hook = Arc::clone(&calls);
        let fetcher = PinGuideFetcher::with_config(PinGuideConfig {
            fetch_json: Some(Arc::new(|_| {
                Ok(json!({
                    "retcode": 0,
                    "data": {
                        "info": {
                            "content": "Animated guide",
                            "img": "https://upload-os-bbs.hoyoverse.com/demo.gif"
                        }
                    }
                }))
            })),
            fetch_bytes: Some(Arc::new(move |url| {
                calls_hook.lock().unwrap().push(url.to_owned());
                if url.contains("x-oss-process") {
                    Ok(Vec::new()) // empty body must fall back to original URL
                } else {
                    Ok(b"GIF89a_FALLBACK".to_vec())
                }
            })),
            ..Default::default()
        });

        let g = fetcher.fetch_sync("777", 2, Some("en"));
        assert!(g.ok);
        assert_eq!(g.image_bytes.as_deref(), Some(b"GIF89a_FALLBACK".as_slice()));
        let recorded = calls.lock().unwrap().clone();
        assert_eq!(recorded.len(), 2);
        assert!(recorded[0].contains("x-oss-process"));
        assert_eq!(recorded[1], "https://upload-os-bbs.hoyoverse.com/demo.gif");

        // 2. Failed image download with non-empty image_url must NOT be cached so request() retries.
        let attempt = Arc::new(AtomicUsize::new(0));
        let attempt_hook = Arc::clone(&attempt);
        let retry_fetcher = PinGuideFetcher::with_config(PinGuideConfig {
            fetch_json: Some(Arc::new(|_| {
                Ok(json!({
                    "retcode": 0,
                    "data": {
                        "info": {
                            "content": "",
                            "img": "https://upload-os-bbs.hoyoverse.com/retry.gif"
                        }
                    }
                }))
            })),
            fetch_bytes: Some(Arc::new(move |_| {
                let n = attempt_hook.fetch_add(1, Ordering::SeqCst);
                if n < 2 {
                    Err("HTTP 504".to_owned())
                } else {
                    Ok(b"GIF89a_OK".to_vec())
                }
            })),
            ..Default::default()
        });

        let (tx1, rx1) = std::sync::mpsc::channel();
        assert!(retry_fetcher.request("888", 2, move |g| { let _ = tx1.send(g); }, Some("vi")));
        let first = rx1.recv_timeout(Duration::from_secs(3)).unwrap();
        assert!(first.ok);
        assert!(first.image_bytes.is_none());
        assert!(retry_fetcher.get_cached("888", Some("vi")).is_none());

        let (tx2, rx2) = std::sync::mpsc::channel();
        assert!(retry_fetcher.request("888", 2, move |g| { let _ = tx2.send(g); }, Some("vi")));
        let second = rx2.recv_timeout(Duration::from_secs(3)).unwrap();
        assert!(second.ok);
        assert_eq!(second.image_bytes.as_deref(), Some(b"GIF89a_OK".as_slice()));
        assert!(retry_fetcher.get_cached("888", Some("vi")).is_some());
    }

    #[test]
    fn byte_cap_evicts_oldest_large_guide_images() {
        let fetcher = PinGuideFetcher::with_config(PinGuideConfig {
            fetch_json: Some(Arc::new(|_| {
                Ok(json!({
                    "retcode": 0,
                    "data": {
                        "info": {
                            "content": "big guide",
                            "img": "https://upload-os-bbs.hoyoverse.com/big.png"
                        }
                    }
                }))
            })),
            fetch_bytes: Some(Arc::new(|_| Ok(vec![0xAB; 6 * 1024 * 1024]))),
            ..Default::default()
        });

        for pid in ["101", "102", "103"] {
            let (tx, rx) = std::sync::mpsc::channel();
            assert!(fetcher.request(pid, 2, move |g| { let _ = tx.send(g); }, Some("vi")));
            let g = rx.recv_timeout(Duration::from_secs(3)).unwrap();
            assert!(g.ok);
        }

        // 3 x 6 MiB = 18 MiB > MAX_CACHE_IMAGE_BYTES (16 MiB), so "101" must be evicted.
        assert!(fetcher.get_cached("101", Some("vi")).is_none());
        assert!(fetcher.get_cached("102", Some("vi")).is_some());
        assert!(fetcher.get_cached("103", Some("vi")).is_some());
        let st = fetcher.inner.state.lock().unwrap();
        assert!(st.cache_image_bytes <= MAX_CACHE_IMAGE_BYTES);
    }
}
