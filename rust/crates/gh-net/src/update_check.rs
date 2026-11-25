//! Opt-in, manual GitHub Releases update checker with pure-Rust FIPS 180-4 SHA-256 verification.
//!
//! Design invariants:
//! - Strictly manual/opt-in: zero background polling, zero telemetry, zero auto-download or exec.
//! - Zero external crypto dependencies: implements FIPS 180-4 SHA-256 directly in pure Rust.
//! - Fail-closed transport validation: enforces HTTPS on non-loopback hosts and allowlists
//!   GitHub release/asset hosts (`github.com`, `objects.githubusercontent.com`,
//!   `release-assets.githubusercontent.com`), permitting `127.0.0.1` only for loopback test servers.

use reqwest::header::{ACCEPT, HeaderMap, HeaderName, HeaderValue, USER_AGENT};
use reqwest::{Url, redirect};
use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{self, Read};
use std::net::IpAddr;
use std::path::Path;
use std::time::Duration;
use subtle::ConstantTimeEq;

/// Canonical GitHub repository slug (`owner/repo`) for official Genshin-Helper releases.
pub const DEFAULT_GITHUB_REPO: &str = "tuanminh26596/Genshin-helper";

/// Default GitHub REST API base URL.
pub const DEFAULT_GITHUB_API_BASE: &str = "https://api.github.com";

/// Default HTTP timeout (in seconds) for manual update checks.
pub const DEFAULT_TIMEOUT_SECS: u64 = 10;

/// Maximum response size accepted for the GitHub release JSON payload (1 MiB).
pub const MAX_RELEASE_JSON_BYTES: usize = 1024 * 1024;

/// Maximum response size accepted for `SHA256SUMS.txt` (256 KiB).
pub const MAX_SHA256SUMS_BYTES: usize = 256 * 1024;

const HEADER_GITHUB_API_VERSION: HeaderName = HeaderName::from_static("x-github-api-version");
const INSTALLER_ASSET_PREFIX: &str = "GenshinHelper-Setup-";
const INSTALLER_ASSET_SUFFIX: &str = ".exe";
const CHECKSUM_ASSET_NAME: &str = "SHA256SUMS.txt";
const FILE_HASH_BUFFER_BYTES: usize = 64 * 1024;

// ---------------------------------------------------------------------------
// Pure-Rust FIPS 180-4 SHA-256
// ---------------------------------------------------------------------------

const SHA256_H0: [u32; 8] = [
    0x6a09_e667,
    0xbb67_ae85,
    0x3c6e_f372,
    0xa54f_f53a,
    0x510e_527f,
    0x9b05_688c,
    0x1f83_d9ab,
    0x5be0_cd19,
];

const SHA256_K: [u32; 64] = [
    0x428a_2f98, 0x7137_4491, 0xb5c0_fbcf, 0xe9b5_dba5,
    0x3956_c25b, 0x59f1_11f1, 0x923f_82a4, 0xab1c_5ed5,
    0xd807_aa98, 0x1283_5b01, 0x2431_85be, 0x550c_7dc3,
    0x72be_5d74, 0x80de_b1fe, 0x9bdc_06a7, 0xc19b_f174,
    0xe49b_69c1, 0xefbe_4786, 0x0fc1_9dc6, 0x240c_a1cc,
    0x2de9_2c6f, 0x4a74_84aa, 0x5cb0_a9dc, 0x76f9_88da,
    0x983e_5152, 0xa831_c66d, 0xb003_27c8, 0xbf59_7fc7,
    0xc6e0_0bf3, 0xd5a7_9147, 0x06ca_6351, 0x1429_2967,
    0x27b7_0a85, 0x2e1b_2138, 0x4d2c_6dfc, 0x5338_0d13,
    0x650a_7354, 0x766a_0abb, 0x81c2_c92e, 0x9272_2c85,
    0xa2bf_e8a1, 0xa81a_664b, 0xc24b_8b70, 0xc76c_51a3,
    0xd192_e819, 0xd699_0624, 0xf40e_3585, 0x106a_a070,
    0x19a4_c116, 0x1e37_6c08, 0x2748_774c, 0x34b0_bcb5,
    0x391c_0cb3, 0x4ed8_aa4a, 0x5b9c_ca4f, 0x682e_6ff3,
    0x748f_82ee, 0x78a5_636f, 0x84c8_7814, 0x8cc7_0208,
    0x90be_fffa, 0xa450_6ceb, 0xbef9_a3f7, 0xc671_78f2,
];

/// Streaming pure-Rust FIPS 180-4 SHA-256 hasher.
#[derive(Debug, Clone)]
pub struct Sha256 {
    state: [u32; 8],
    buf: [u8; 64],
    buf_len: usize,
    total_bytes: u64,
}

impl Default for Sha256 {
    fn default() -> Self {
        Self::new()
    }
}

impl Sha256 {
    pub fn new() -> Self {
        Self {
            state: SHA256_H0,
            buf: [0u8; 64],
            buf_len: 0,
            total_bytes: 0,
        }
    }

    pub fn update(&mut self, mut data: &[u8]) {
        if data.is_empty() {
            return;
        }
        self.total_bytes = self.total_bytes.wrapping_add(data.len() as u64);

        if self.buf_len > 0 {
            let need = 64 - self.buf_len;
            let take = need.min(data.len());
            self.buf[self.buf_len..self.buf_len + take].copy_from_slice(&data[..take]);
            self.buf_len += take;
            data = &data[take..];

            if self.buf_len == 64 {
                let block = self.buf;
                self.compress(&block);
                self.buf_len = 0;
            }
        }

        while data.len() >= 64 {
            let (chunk, rest) = data.split_at(64);
            let block: &[u8; 64] = chunk.try_into().expect("64-byte SHA-256 block");
            self.compress(block);
            data = rest;
        }

        if !data.is_empty() {
            self.buf[..data.len()].copy_from_slice(data);
            self.buf_len = data.len();
        }
    }

    pub fn finalize(mut self) -> [u8; 32] {
        let bit_len = self.total_bytes.wrapping_mul(8);

        self.buf[self.buf_len] = 0x80;
        self.buf_len += 1;

        if self.buf_len > 56 {
            self.buf[self.buf_len..64].fill(0);
            let block = self.buf;
            self.compress(&block);
            self.buf_len = 0;
        }

        self.buf[self.buf_len..56].fill(0);
        self.buf[56..64].copy_from_slice(&bit_len.to_be_bytes());
        let final_block = self.buf;
        self.compress(&final_block);

        let mut out = [0u8; 32];
        for (chunk, word) in out.as_chunks_mut::<4>().0.iter_mut().zip(self.state) {
            *chunk = word.to_be_bytes();
        }
        out
    }

    pub fn finalize_hex(self) -> String {
        encode_hex_lower(&self.finalize())
    }

    fn compress(&mut self, block: &[u8; 64]) {
        let mut w = [0u32; 64];
        for (slot, chunk) in w[..16].iter_mut().zip(block.as_chunks::<4>().0) {
            *slot = u32::from_be_bytes(*chunk);
        }
        for t in 16..64 {
            let s0 = w[t - 15].rotate_right(7) ^ w[t - 15].rotate_right(18) ^ (w[t - 15] >> 3);
            let s1 = w[t - 2].rotate_right(17) ^ w[t - 2].rotate_right(19) ^ (w[t - 2] >> 10);
            w[t] = w[t - 16]
                .wrapping_add(s0)
                .wrapping_add(w[t - 7])
                .wrapping_add(s1);
        }

        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut h] = self.state;
        for (&k, &wt) in SHA256_K.iter().zip(w.iter()) {
            let cap_s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = h
                .wrapping_add(cap_s1)
                .wrapping_add(ch)
                .wrapping_add(k)
                .wrapping_add(wt);
            let cap_s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = cap_s0.wrapping_add(maj);

            h = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }

        self.state[0] = self.state[0].wrapping_add(a);
        self.state[1] = self.state[1].wrapping_add(b);
        self.state[2] = self.state[2].wrapping_add(c);
        self.state[3] = self.state[3].wrapping_add(d);
        self.state[4] = self.state[4].wrapping_add(e);
        self.state[5] = self.state[5].wrapping_add(f);
        self.state[6] = self.state[6].wrapping_add(g);
        self.state[7] = self.state[7].wrapping_add(h);
    }
}

/// Computes the FIPS 180-4 SHA-256 digest of `data` as 32 raw bytes.
pub fn sha256_bytes(data: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(data);
    hasher.finalize()
}

/// Computes the lowercase 64-character hexadecimal SHA-256 digest of `data`.
pub fn sha256_hex(data: &[u8]) -> String {
    encode_hex_lower(&sha256_bytes(data))
}

/// Streams a file on disk and returns its lowercase 64-character SHA-256 hex digest.
pub fn sha256_file(path: &Path) -> io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; FILE_HASH_BUFFER_BYTES];
    loop {
        match file.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => hasher.update(&buf[..n]),
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err),
        }
    }
    Ok(hasher.finalize_hex())
}

fn encode_hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

fn is_valid_sha256_hex(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

// ---------------------------------------------------------------------------
// SHA256SUMS.txt parsing & file verification
// ---------------------------------------------------------------------------

/// Parses standard GNU `sha256sum` manifests into a `filename -> lowercase_sha256` map.
///
/// Supports both `<64-hex>  <filename>` (text mode) and `<64-hex> *<filename>` (binary mode),
/// strips an optional UTF-8 BOM, ignores blank or `#` comment lines, normalizes digests to
/// lowercase hex, and silently skips lines with malformed hashes or missing filenames.
pub fn parse_sha256sums(content: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    let clean = content.strip_prefix('\u{feff}').unwrap_or(content);

    for raw_line in clean.lines() {
        let line = raw_line.trim_end_matches('\r');
        let trimmed_start = line.trim_start();
        if trimmed_start.is_empty() || trimmed_start.starts_with('#') {
            continue;
        }

        let Some((digest_part, remainder)) = split_leading_sha256_token(trimmed_start) else {
            continue;
        };
        if !is_valid_sha256_hex(digest_part) {
            continue;
        }

        let filename_raw = if let Some(rest) = remainder.strip_prefix("  ") {
            rest
        } else if let Some(rest) = remainder.strip_prefix(" *") {
            rest
        } else if remainder.starts_with(' ') || remainder.starts_with('\t') {
            let trimmed_ws = remainder.trim_start_matches([' ', '\t']);
            trimmed_ws.strip_prefix('*').unwrap_or(trimmed_ws)
        } else {
            continue;
        };

        let filename = filename_raw.trim_end_matches([' ', '\t']);
        if filename.is_empty() {
            continue;
        }

        out.insert(filename.to_owned(), digest_part.to_ascii_lowercase());
    }

    out
}

fn split_leading_sha256_token(s: &str) -> Option<(&str, &str)> {
    let sep_idx = s.find([' ', '\t'])?;
    Some((&s[..sep_idx], &s[sep_idx..]))
}

/// Verifies that the file at `path` matches `expected_hex` (64-char ASCII hex SHA-256).
///
/// Uses constant-time digest comparison and returns `Err` if `expected_hex` is malformed or the
/// file cannot be read.
pub fn verify_file_sha256(path: &Path, expected_hex: &str) -> Result<bool, String> {
    let trimmed = expected_hex.trim();
    if !is_valid_sha256_hex(trimmed) {
        return Err(format!(
            "invalid expected SHA-256 hex digest (expected 64 hex chars, got {})",
            trimmed.len()
        ));
    }
    let expected_lower = trimmed.to_ascii_lowercase();
    let actual_lower =
        sha256_file(path).map_err(|e| format!("failed to hash {}: {e}", path.display()))?;
    let eq = actual_lower
        .as_bytes()
        .ct_eq(expected_lower.as_bytes());
    Ok(bool::from(eq))
}

// ---------------------------------------------------------------------------
// SemVer parsing & comparison
// ---------------------------------------------------------------------------

/// Parses a `MAJOR.MINOR.PATCH` version string (with optional leading `v`/`V`, pre-release,
/// or build metadata suffix) into `(major, minor, patch)`.
pub fn parse_semver(tag: &str) -> Option<(u64, u64, u64)> {
    let (major, minor, patch, _) = parse_semver_parts(tag)?;
    Some((major, minor, patch))
}

/// Returns `true` if `latest` represents a strictly newer SemVer version than `current`.
///
/// Handles optional leading `v`/`V`, ignores `+build` metadata, and follows SemVer 2.0.0
/// precedence for pre-release suffixes (e.g. `5.6.0 > 5.6.0-rc.1`). Returns `false` if either
/// version string is not a valid `MAJOR.MINOR.PATCH` version.
pub fn is_newer_version(current: &str, latest: &str) -> bool {
    let Some((cur_maj, cur_min, cur_pat, cur_pre)) = parse_semver_parts(current) else {
        return false;
    };
    let Some((lat_maj, lat_min, lat_pat, lat_pre)) = parse_semver_parts(latest) else {
        return false;
    };

    match (lat_maj, lat_min, lat_pat).cmp(&(cur_maj, cur_min, cur_pat)) {
        Ordering::Greater => true,
        Ordering::Less => false,
        Ordering::Equal => match (cur_pre, lat_pre) {
            (Some(_), None) => true,
            (None, Some(_)) | (None, None) => false,
            (Some(c), Some(l)) => compare_prerelease(l, c) == Ordering::Greater,
        },
    }
}

fn parse_semver_parts(tag: &str) -> Option<(u64, u64, u64, Option<&str>)> {
    let trimmed = tag.trim();
    let without_v = trimmed
        .strip_prefix('v')
        .or_else(|| trimmed.strip_prefix('V'))
        .unwrap_or(trimmed);
    let without_build = without_v
        .split_once('+')
        .map_or(without_v, |(before, _)| before);
    let (core, pre) = match without_build.split_once('-') {
        Some((c, p)) if !p.is_empty() => (c, Some(p)),
        Some(_) => return None,
        None => (without_build, None),
    };

    let mut parts = core.split('.');
    let major = parse_decimal_u64(parts.next()?)?;
    let minor = parse_decimal_u64(parts.next()?)?;
    let patch = parse_decimal_u64(parts.next()?)?;
    if parts.next().is_some() {
        return None;
    }

    Some((major, minor, patch, pre))
}

fn parse_decimal_u64(s: &str) -> Option<u64> {
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse::<u64>().ok()
}

fn compare_prerelease(a: &str, b: &str) -> Ordering {
    let mut a_it = a.split('.');
    let mut b_it = b.split('.');
    loop {
        match (a_it.next(), b_it.next()) {
            (Some(ia), Some(ib)) => {
                let a_num = parse_decimal_u128(ia);
                let b_num = parse_decimal_u128(ib);
                let ord = match (a_num, b_num) {
                    (Some(na), Some(nb)) => na.cmp(&nb),
                    (Some(_), None) => Ordering::Less,
                    (None, Some(_)) => Ordering::Greater,
                    (None, None) => ia.cmp(ib),
                };
                if ord != Ordering::Equal {
                    return ord;
                }
            }
            (None, None) => return Ordering::Equal,
            (None, Some(_)) => return Ordering::Less,
            (Some(_), None) => return Ordering::Greater,
        }
    }
}

fn parse_decimal_u128(s: &str) -> Option<u128> {
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse::<u128>().ok()
}

// ---------------------------------------------------------------------------
// Manual / Opt-In GitHub Releases Update Checker
// ---------------------------------------------------------------------------

/// Metadata for an individual GitHub Release downloadable asset.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReleaseAsset {
    pub name: String,
    pub download_url: String,
    pub size: u64,
}

/// Result of a manual check against GitHub Releases.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpdateCheckResult {
    pub current_version: String,
    pub latest_version: String,
    pub has_update: bool,
    pub release_url: String,
    pub installer_asset: Option<ReleaseAsset>,
    pub expected_sha256: Option<String>,
    pub checksum_verified_present: bool,
}

/// Configuration for [`UpdateCheckClient`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpdateCheckConfig {
    pub repo: String,
    pub api_base_url: String,
    pub timeout_secs: u64,
    pub user_agent: String,
}

impl Default for UpdateCheckConfig {
    fn default() -> Self {
        Self {
            repo: DEFAULT_GITHUB_REPO.to_owned(),
            api_base_url: DEFAULT_GITHUB_API_BASE.to_owned(),
            timeout_secs: DEFAULT_TIMEOUT_SECS,
            user_agent: format!("GenshinHelper/{}", gh_core::APP_VERSION),
        }
    }
}

/// Manual, zero-telemetry GitHub Releases update checker client.
///
/// Only performs network I/O when the caller explicitly invokes [`Self::check_latest`].
/// Never spawns background polling tasks, never downloads installers, and never executes code.
#[derive(Debug, Clone, Default)]
pub struct UpdateCheckClient {
    config: UpdateCheckConfig,
}

impl UpdateCheckClient {
    pub fn new(config: UpdateCheckConfig) -> Self {
        Self { config }
    }

    pub fn config(&self) -> &UpdateCheckConfig {
        &self.config
    }

    /// Queries `{api_base_url}/repos/{repo}/releases/latest` once and checks whether a newer
    /// release exists along with its installer asset and published `SHA256SUMS.txt` digest.
    pub fn check_latest(&self, current_version: &str) -> Result<UpdateCheckResult, String> {
        let repo = validate_repo_slug(&self.config.repo)?;
        let (api_base, allow_loopback) = validate_api_base_url(&self.config.api_base_url)?;
        let timeout = Duration::from_secs(self.config.timeout_secs.max(1));
        let user_agent = self.config.user_agent.trim().to_owned();
        if user_agent.is_empty() {
            return Err("update checker user_agent must not be empty".to_owned());
        }

        let base_trimmed = api_base.as_str().trim_end_matches('/');
        let release_api_url = format!("{base_trimmed}/repos/{repo}/releases/latest");
        let api_host = api_base.host_str().unwrap_or_default().to_ascii_lowercase();

        let release_bytes = http_get_bytes(HttpGetRequest {
            url: &release_api_url,
            user_agent: &user_agent,
            accept: "application/vnd.github+json",
            include_github_api_version: true,
            timeout,
            limit: MAX_RELEASE_JSON_BYTES,
            allow_loopback,
            redirect_scope: RedirectScope::ApiHost(api_host),
        })?;

        let release: GitHubReleaseDto = serde_json::from_slice(&release_bytes)
            .map_err(|e| format!("failed to parse GitHub release JSON: {e}"))?;

        let latest_version = release.tag_name.trim().to_owned();
        if latest_version.is_empty() {
            return Err("GitHub release payload missing tag_name".to_owned());
        }

        let release_url = if release.html_url.trim().is_empty() {
            format!("https://github.com/{repo}/releases")
        } else {
            let trimmed_url = release.html_url.trim();
            if !is_allowed_release_page_url(trimmed_url, allow_loopback) {
                return Err(format!("untrusted GitHub release URL: {trimmed_url}"));
            }
            trimmed_url.to_owned()
        };

        let mut installer_asset: Option<ReleaseAsset> = None;
        let mut checksum_asset_url: Option<String> = None;

        for asset in release.assets {
            let name = asset.name.trim();
            let dl_url = asset.browser_download_url.trim();

            if installer_asset.is_none() && is_installer_asset_name(name) {
                if !is_allowed_asset_url(dl_url, allow_loopback) {
                    return Err(format!("untrusted installer download URL: {dl_url}"));
                }
                installer_asset = Some(ReleaseAsset {
                    name: name.to_owned(),
                    download_url: dl_url.to_owned(),
                    size: asset.size,
                });
            } else if checksum_asset_url.is_none() && name == CHECKSUM_ASSET_NAME {
                if !is_allowed_asset_url(dl_url, allow_loopback) {
                    return Err(format!("untrusted checksum download URL: {dl_url}"));
                }
                checksum_asset_url = Some(dl_url.to_owned());
            }
        }

        let expected_sha256 = if let (Some(installer), Some(sums_url)) =
            (installer_asset.as_ref(), checksum_asset_url.as_deref())
        {
            let sums_bytes = http_get_bytes(HttpGetRequest {
                url: sums_url,
                user_agent: &user_agent,
                accept: "text/plain, application/octet-stream",
                include_github_api_version: false,
                timeout,
                limit: MAX_SHA256SUMS_BYTES,
                allow_loopback,
                redirect_scope: RedirectScope::ReleaseAsset,
            })?;
            let sums_text = String::from_utf8(sums_bytes)
                .map_err(|e| format!("SHA256SUMS.txt is not valid UTF-8: {e}"))?;
            let parsed = parse_sha256sums(&sums_text);
            lookup_asset_checksum(&parsed, &installer.name)
        } else {
            None
        };

        let checksum_verified_present = expected_sha256.is_some();
        let has_update = is_newer_version(current_version, &latest_version);

        Ok(UpdateCheckResult {
            current_version: current_version.trim().to_owned(),
            latest_version,
            has_update,
            release_url,
            installer_asset,
            expected_sha256,
            checksum_verified_present,
        })
    }
}

#[derive(Debug, Deserialize)]
struct GitHubReleaseDto {
    #[serde(default)]
    tag_name: String,
    #[serde(default)]
    html_url: String,
    #[serde(default)]
    assets: Vec<GitHubAssetDto>,
}

#[derive(Debug, Deserialize)]
struct GitHubAssetDto {
    #[serde(default)]
    name: String,
    #[serde(default)]
    browser_download_url: String,
    #[serde(default)]
    size: u64,
}

#[derive(Debug, Clone)]
enum RedirectScope {
    ApiHost(String),
    ReleaseAsset,
}

fn is_installer_asset_name(name: &str) -> bool {
    name.starts_with(INSTALLER_ASSET_PREFIX)
        && name.len() > INSTALLER_ASSET_PREFIX.len() + INSTALLER_ASSET_SUFFIX.len()
        && name
            .get(name.len() - INSTALLER_ASSET_SUFFIX.len()..)
            .is_some_and(|suffix| suffix.eq_ignore_ascii_case(INSTALLER_ASSET_SUFFIX))
}

fn lookup_asset_checksum(
    parsed: &BTreeMap<String, String>,
    installer_name: &str,
) -> Option<String> {
    if let Some(exact) = parsed.get(installer_name) {
        return Some(exact.clone());
    }
    parsed
        .iter()
        .find(|(entry_name, _)| {
            entry_name
                .rsplit(['/', '\\'])
                .next()
                .is_some_and(|leaf| leaf == installer_name)
        })
        .map(|(_, digest)| digest.clone())
}

fn validate_repo_slug(raw: &str) -> Result<&str, String> {
    let slug = raw.trim();
    let Some((owner, name)) = slug.split_once('/') else {
        return Err(format!("invalid GitHub repository slug: {slug}"));
    };
    if owner.is_empty()
        || name.is_empty()
        || name.contains('/')
        || owner == "."
        || owner == ".."
        || name == "."
        || name == ".."
        || !is_safe_repo_segment(owner)
        || !is_safe_repo_segment(name)
    {
        return Err(format!("invalid GitHub repository slug: {slug}"));
    }
    Ok(slug)
}

fn is_safe_repo_segment(seg: &str) -> bool {
    seg.bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
}

fn is_loopback_host_str(host: &str) -> bool {
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    bare.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback()) || host.eq_ignore_ascii_case("localhost")
}

fn validate_api_base_url(raw: &str) -> Result<(Url, bool), String> {
    let url = Url::parse(raw.trim()).map_err(|e| format!("invalid api_base_url: {e}"))?;
    if !url.username().is_empty() || url.password().is_some() {
        return Err("api_base_url must not contain embedded credentials".to_owned());
    }
    let Some(host) = url.host_str() else {
        return Err("api_base_url missing host".to_owned());
    };
    let is_loopback = is_loopback_host_str(host);
    match url.scheme() {
        "https" => Ok((url, is_loopback)),
        "http" if is_loopback => Ok((url, true)),
        other => Err(format!(
            "api_base_url must use https for non-loopback hosts (got {other}://{host})"
        )),
    }
}

/// Validates whether `raw` is a trusted GitHub release/asset download URL.
///
/// Accepts HTTPS URLs hosted on `github.com`, `objects.githubusercontent.com`, or
/// `release-assets.githubusercontent.com`. Loopback (`127.0.0.1` / `[::1]` / `localhost`)
/// HTTP/HTTPS URLs are accepted only when `allow_loopback` is `true` (offline mock tests).
pub fn is_allowed_asset_url(raw: &str, allow_loopback: bool) -> bool {
    let Ok(url) = Url::parse(raw.trim()) else {
        return false;
    };
    if !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    let Some(host) = url.host_str() else {
        return false;
    };
    if allow_loopback && is_loopback_host_str(host) {
        return matches!(url.scheme(), "http" | "https");
    }
    url.scheme() == "https"
        && (host.eq_ignore_ascii_case("github.com")
            || host.eq_ignore_ascii_case("objects.githubusercontent.com")
            || host.eq_ignore_ascii_case("release-assets.githubusercontent.com"))
}

fn is_allowed_release_page_url(raw: &str, allow_loopback: bool) -> bool {
    let Ok(url) = Url::parse(raw.trim()) else {
        return false;
    };
    if !url.username().is_empty() || url.password().is_some() {
        return false;
    }
    let Some(host) = url.host_str() else {
        return false;
    };
    if allow_loopback && is_loopback_host_str(host) {
        return matches!(url.scheme(), "http" | "https");
    }
    url.scheme() == "https" && host.eq_ignore_ascii_case("github.com")
}

struct HttpGetRequest<'a> {
    url: &'a str,
    user_agent: &'a str,
    accept: &'static str,
    include_github_api_version: bool,
    timeout: Duration,
    limit: usize,
    allow_loopback: bool,
    redirect_scope: RedirectScope,
}

fn http_get_bytes(req: HttpGetRequest<'_>) -> Result<Vec<u8>, String> {
    let url_owned = req.url.to_owned();
    let ua_owned = req.user_agent.to_owned();
    let accept = req.accept;
    let include_github_api_version = req.include_github_api_version;
    let timeout = req.timeout;
    let limit = req.limit;
    let allow_loopback = req.allow_loopback;
    let redirect_scope = req.redirect_scope;

    run_blocking_result(move || {
        let mut headers = HeaderMap::new();
        let ua_val = HeaderValue::from_str(&ua_owned)
            .map_err(|e| format!("invalid user_agent header: {e}"))?;
        headers.insert(USER_AGENT, ua_val);
        headers.insert(ACCEPT, HeaderValue::from_static(accept));
        if include_github_api_version {
            headers.insert(
                HEADER_GITHUB_API_VERSION,
                HeaderValue::from_static("2022-11-28"),
            );
        }

        let policy = redirect::Policy::custom(move |attempt| {
            if attempt.previous().len() >= 5 {
                return attempt.error("too many redirects");
            }
            let target = attempt.url();
            let allowed = match &redirect_scope {
                RedirectScope::ReleaseAsset => is_allowed_asset_url(target.as_str(), allow_loopback),
                RedirectScope::ApiHost(expected_host) => {
                    let Some(host) = target.host_str() else {
                        return attempt.error("redirect target missing host");
                    };
                    if !target.username().is_empty() || target.password().is_some() {
                        false
                    } else if allow_loopback && is_loopback_host_str(host) {
                        matches!(target.scheme(), "http" | "https")
                    } else {
                        target.scheme() == "https"
                            && (host.eq_ignore_ascii_case(expected_host)
                                || host.eq_ignore_ascii_case("api.github.com"))
                    }
                }
            };
            if allowed {
                attempt.follow()
            } else {
                attempt.error("redirect target blocked by host/scheme allowlist")
            }
        });

        let mut builder = reqwest::blocking::Client::builder()
            .connect_timeout(timeout)
            .timeout(timeout)
            .redirect(policy);
        if allow_loopback {
            builder = builder.no_proxy();
        }
        let client = builder
            .build()
            .map_err(|e| format!("failed to build HTTP client: {e}"))?;

        let resp = client
            .get(&url_owned)
            .headers(headers)
            .send()
            .map_err(|e| format!("HTTP request failed: {e}"))?;
        if !resp.status().is_success() {
            return Err(format!("HTTP status {}", resp.status()));
        }

        let mut buf = Vec::new();
        let mut limited = resp.take((limit + 1) as u64);
        limited
            .read_to_end(&mut buf)
            .map_err(|e| format!("failed to read HTTP body: {e}"))?;
        if buf.len() > limit {
            return Err(format!("HTTP response exceeded {limit}-byte safety limit"));
        }
        Ok(buf)
    })
}

fn run_blocking_result<F, T>(f: F) -> Result<T, String>
where
    F: FnOnce() -> Result<T, String> + Send + 'static,
    T: Send + 'static,
{
    if tokio::runtime::Handle::try_current().is_ok() {
        let handle = std::thread::Builder::new()
            .name("gh-update-check".to_owned())
            .spawn(f)
            .map_err(|e| format!("failed to spawn update-check thread: {e}"))?;
        handle
            .join()
            .map_err(|_| "update-check thread panicked".to_owned())?
    } else {
        f()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::net::TcpListener;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static TEMP_SEQ: AtomicUsize = AtomicUsize::new(0);

    fn temp_file_path(label: &str) -> std::path::PathBuf {
        let seq = TEMP_SEQ.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "gh_net_update_check_{label}_{}_{seq}.tmp",
            std::process::id()
        ))
    }

    fn spawn_mock_http_server(
        routes: Vec<(String, u16, &'static str, String)>,
        expected_requests: usize,
    ) -> (String, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback mock server");
        let addr = listener.local_addr().expect("mock server local_addr");
        let base_url = format!("http://{addr}");

        let handle = std::thread::spawn(move || {
            for _ in 0..expected_requests {
                let (mut stream, _) = listener.accept().expect("accept mock connection");
                let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
                let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));

                let mut req_buf = Vec::new();
                let mut chunk = [0u8; 1024];
                loop {
                    match stream.read(&mut chunk) {
                        Ok(0) => break,
                        Ok(n) => {
                            req_buf.extend_from_slice(&chunk[..n]);
                            if req_buf.windows(4).any(|w| w == b"\r\n\r\n") || req_buf.len() > 16384
                            {
                                break;
                            }
                        }
                        Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
                        Err(_) => break,
                    }
                }

                let req_str = String::from_utf8_lossy(&req_buf);
                let path = req_str
                    .lines()
                    .next()
                    .and_then(|line| line.split_whitespace().nth(1))
                    .unwrap_or("/");

                let (status, content_type, body) = routes
                    .iter()
                    .find(|(route_path, _, _, _)| route_path == path)
                    .map(|(_, st, ct, b)| (*st, *ct, b.as_str()))
                    .unwrap_or((404, "text/plain", "not found"));

                let reason = match status {
                    200 => "OK",
                    404 => "Not Found",
                    500 => "Internal Server Error",
                    _ => "Status",
                };
                let resp = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(resp.as_bytes());
                let _ = stream.flush();
            }
        });

        (base_url, handle)
    }

    #[test]
    fn sha256_nist_fips_180_4_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            sha256_hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
        assert_eq!(
            sha256_hex(
                b"abcdefghbcdefghicdefghijdefghijkefghijklfghijklmghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrsmnopqrstnopqrstu"
            ),
            "cf5b16a778af8380036ce59e7b0492370b249b11e8f07a51afac45037afee9d1"
        );

        // Verify streaming chunked updates match single-shot across block boundaries (55..66, 130).
        for len in [0usize, 1, 55, 56, 57, 63, 64, 65, 128, 130] {
            let payload: Vec<u8> = (0..len).map(|i| ((i * 31 + 7) & 0xff) as u8).collect();
            let direct = sha256_bytes(&payload);

            let mut streamed = Sha256::new();
            for chunk in payload.chunks(7) {
                streamed.update(chunk);
            }
            assert_eq!(streamed.finalize(), direct, "chunked mismatch at len={len}");
        }
    }

    #[test]
    fn parse_sha256sums_supports_text_binary_bom_and_rejects_invalid() {
        let h1 = "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD";
        let h2 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
        let content = format!(
            "\u{feff}# Release checksums\r\n\
             \r\n\
             {h1}  GenshinHelper-Setup-v5.7.0.exe\r\n\
             {h2} *Genshin Helper Portable.zip\n\
             deadbeef  too-short.exe\n\
             zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz  non-hex.exe\n\
             {h2}no-separator.exe\n\
             {h2}  \n"
        );

        let parsed = parse_sha256sums(&content);
        assert_eq!(parsed.len(), 2);
        assert_eq!(
            parsed.get("GenshinHelper-Setup-v5.7.0.exe").map(String::as_str),
            Some("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        );
        assert_eq!(
            parsed.get("Genshin Helper Portable.zip").map(String::as_str),
            Some(h2)
        );
    }

    #[test]
    fn verify_file_sha256_matches_mismatches_and_validates_hex() {
        let path = temp_file_path("verify");
        std::fs::write(&path, b"abc").expect("write temp file");

        let abc_lower = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        let abc_upper = "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD";
        let wrong_hex = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

        assert_eq!(sha256_file(&path).unwrap(), abc_lower);
        assert_eq!(verify_file_sha256(&path, abc_lower), Ok(true));
        assert_eq!(verify_file_sha256(&path, abc_upper), Ok(true));
        assert_eq!(verify_file_sha256(&path, wrong_hex), Ok(false));
        assert!(verify_file_sha256(&path, "not-a-valid-sha256").is_err());

        let _ = std::fs::remove_file(&path);
        assert!(verify_file_sha256(&path, abc_lower).is_err());
    }

    #[test]
    fn parse_semver_and_is_newer_version_behavior() {
        assert_eq!(parse_semver("v5.6.0"), Some((5, 6, 0)));
        assert_eq!(parse_semver("5.6.0"), Some((5, 6, 0)));
        assert_eq!(parse_semver("V5.6.1-rc.1+build.42"), Some((5, 6, 1)));
        assert_eq!(parse_semver("5.6"), None);
        assert_eq!(parse_semver("5.6.0.1"), None);
        assert_eq!(parse_semver("v5.6.0-"), None);
        assert_eq!(parse_semver("not-semver"), None);

        assert!(is_newer_version("5.6.0", "v5.6.1"));
        assert!(is_newer_version("v5.6.0", "5.7.0"));
        assert!(is_newer_version("5.6.0", "v6.0.0"));
        assert!(is_newer_version("5.6.0-rc.1", "v5.6.0"));
        assert!(is_newer_version("5.6.0-beta.2", "5.6.0-beta.10"));

        assert!(!is_newer_version("5.6.0", "v5.6.0"));
        assert!(!is_newer_version("5.7.0", "v5.6.9"));
        assert!(!is_newer_version("5.6.0", "v5.6.0-rc.1"));
        assert!(!is_newer_version("5.6.0-beta.10", "5.6.0-beta.2"));
        assert!(!is_newer_version("invalid", "5.7.0"));
        assert!(!is_newer_version("5.6.0", "invalid"));
    }

    #[test]
    fn url_allowlist_enforces_https_and_github_hosts() {
        assert!(is_allowed_asset_url(
            "https://github.com/tuanminh26596/Genshin-helper/releases/download/v5.7.0/GenshinHelper-Setup-v5.7.0.exe",
            false
        ));
        assert!(is_allowed_asset_url(
            "https://objects.githubusercontent.com/github-production-release-asset-123/asset.exe",
            false
        ));
        assert!(is_allowed_asset_url(
            "https://release-assets.githubusercontent.com/github-production-release-asset-123/asset.exe",
            false
        ));

        assert!(!is_allowed_asset_url(
            "http://github.com/tuanminh26596/Genshin-helper/releases/download/v5.7.0/asset.exe",
            false
        ));
        assert!(!is_allowed_asset_url(
            "https://github.com.evil.example/asset.exe",
            false
        ));
        assert!(!is_allowed_asset_url(
            "https://user:pass@github.com/asset.exe",
            false
        ));
        assert!(!is_allowed_asset_url(
            "http://127.0.0.1:8765/GenshinHelper-Setup-v5.7.0.exe",
            false
        ));
        assert!(is_allowed_asset_url(
            "http://127.0.0.1:8765/GenshinHelper-Setup-v5.7.0.exe",
            true
        ));
    }

    #[test]
    fn check_latest_with_loopback_mock_server_extracts_installer_and_sha256() {
        let expected_hash = "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1";
        let setup_name = "GenshinHelper-Setup-v5.7.0.exe";
        let (base_url, handle) = {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback mock server");
            let addr = listener.local_addr().expect("mock server local_addr");
            let base = format!("http://{addr}");
            let setup_dl = format!("{base}/download/{setup_name}");
            let sums_dl = format!("{base}/download/SHA256SUMS.txt");
            let release_json = serde_json::json!({
                "tag_name": "v5.7.0",
                "html_url": "https://github.com/tuanminh26596/Genshin-helper/releases/tag/v5.7.0",
                "assets": [
                    {
                        "name": setup_name,
                        "browser_download_url": setup_dl,
                        "size": 7_450_123u64
                    },
                    {
                        "name": "SHA256SUMS.txt",
                        "browser_download_url": sums_dl,
                        "size": 100u64
                    }
                ]
            })
            .to_string();
            let sums_body = format!("{expected_hash}  {setup_name}\r\n");

            let h = std::thread::spawn(move || {
                for _ in 0..4 {
                    let (mut stream, _) = listener.accept().expect("accept mock connection");
                    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
                    let mut req_buf = Vec::new();
                    let mut chunk = [0u8; 1024];
                    loop {
                        match stream.read(&mut chunk) {
                            Ok(0) => break,
                            Ok(n) => {
                                req_buf.extend_from_slice(&chunk[..n]);
                                if req_buf.windows(4).any(|w| w == b"\r\n\r\n") {
                                    break;
                                }
                            }
                            Err(_) => break,
                        }
                    }
                    let req_str = String::from_utf8_lossy(&req_buf);
                    let path = req_str
                        .lines()
                        .next()
                        .and_then(|l| l.split_whitespace().nth(1))
                        .unwrap_or("/");
                    let (ct, body) = if path == "/repos/tuanminh26596/Genshin-helper/releases/latest"
                    {
                        ("application/json", release_json.as_str())
                    } else if path == "/download/SHA256SUMS.txt" {
                        ("text/plain", sums_body.as_str())
                    } else {
                        ("text/plain", "")
                    };
                    let status = if body.is_empty() {
                        "404 Not Found"
                    } else {
                        "200 OK"
                    };
                    let resp = format!(
                        "HTTP/1.1 {status}\r\nContent-Type: {ct}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = stream.write_all(resp.as_bytes());
                    let _ = stream.flush();
                }
            });
            (base, h)
        };

        let client = UpdateCheckClient::new(UpdateCheckConfig {
            api_base_url: base_url.clone(),
            timeout_secs: 5,
            ..Default::default()
        });

        let res = client
            .check_latest("5.6.0")
            .expect("check_latest should succeed");
        assert_eq!(res.current_version, "5.6.0");
        assert_eq!(res.latest_version, "v5.7.0");
        assert!(res.has_update);
        assert_eq!(
            res.release_url,
            "https://github.com/tuanminh26596/Genshin-helper/releases/tag/v5.7.0"
        );
        assert_eq!(
            res.installer_asset,
            Some(ReleaseAsset {
                name: setup_name.to_owned(),
                download_url: format!("{base_url}/download/{setup_name}"),
                size: 7_450_123,
            })
        );
        assert_eq!(res.expected_sha256.as_deref(), Some(expected_hash));
        assert!(res.checksum_verified_present);

        // Up-to-date check against the same mock server.
        let up_to_date = client
            .check_latest("v5.7.0")
            .expect("up-to-date check should succeed");
        assert!(!up_to_date.has_update);
        assert!(up_to_date.checksum_verified_present);

        handle.join().expect("mock server thread finished cleanly");
    }

    #[test]
    fn check_latest_handles_missing_sha256sums_and_rejects_untrusted_urls() {
        // 1. Non-HTTPS non-loopback api_base_url rejected before any network call.
        let insecure = UpdateCheckClient::new(UpdateCheckConfig {
            api_base_url: "http://api.github.com".to_owned(),
            ..Default::default()
        });
        assert!(insecure.check_latest("5.6.0").is_err());

        // 2. Release without SHA256SUMS.txt succeeds with checksum_verified_present = false.
        let no_sums_json = serde_json::json!({
            "tag_name": "v5.7.0",
            "html_url": "https://github.com/tuanminh26596/Genshin-helper/releases/tag/v5.7.0",
            "assets": [
                {
                    "name": "GenshinHelper-Setup-v5.7.0.exe",
                    "browser_download_url": "https://github.com/tuanminh26596/Genshin-helper/releases/download/v5.7.0/GenshinHelper-Setup-v5.7.0.exe",
                    "size": 12345u64
                }
            ]
        })
        .to_string();

        let (base_ok, handle_ok) = spawn_mock_http_server(
            vec![(
                "/repos/tuanminh26596/Genshin-helper/releases/latest".to_owned(),
                200,
                "application/json",
                no_sums_json,
            )],
            1,
        );
        let ok_client = UpdateCheckClient::new(UpdateCheckConfig {
            api_base_url: base_ok,
            timeout_secs: 5,
            ..Default::default()
        });
        let res = ok_client
            .check_latest("5.6.0")
            .expect("release without SHA256SUMS.txt should succeed");
        assert!(res.installer_asset.is_some());
        assert_eq!(res.expected_sha256, None);
        assert!(!res.checksum_verified_present);
        handle_ok.join().expect("mock ok server finished");

        // 3. Untrusted installer download host fails closed.
        let evil_json = serde_json::json!({
            "tag_name": "v5.7.0",
            "html_url": "https://github.com/tuanminh26596/Genshin-helper/releases/tag/v5.7.0",
            "assets": [
                {
                    "name": "GenshinHelper-Setup-v5.7.0.exe",
                    "browser_download_url": "https://evil.example.com/GenshinHelper-Setup-v5.7.0.exe",
                    "size": 12345u64
                }
            ]
        })
        .to_string();
        let (base_evil, handle_evil) = spawn_mock_http_server(
            vec![(
                "/repos/tuanminh26596/Genshin-helper/releases/latest".to_owned(),
                200,
                "application/json",
                evil_json,
            )],
            1,
        );
        let evil_client = UpdateCheckClient::new(UpdateCheckConfig {
            api_base_url: base_evil,
            timeout_secs: 5,
            ..Default::default()
        });
        let err = evil_client
            .check_latest("5.6.0")
            .expect_err("untrusted asset URL must be rejected");
        assert!(err.contains("untrusted installer download URL"));
        handle_evil.join().expect("mock evil server finished");
    }
}
