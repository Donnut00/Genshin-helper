//! Native DXGI HDR detection + Windows Graphics Capture (WGC) `R16G16B16A16Float` capture.
//!
//! Automatically detects whether a display monitor is running Windows HDR / Auto-HDR via
//! `IDXGIOutput6::GetDesc1` (`DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020`), queries the exact
//! user Windows "SDR content brightness" slider via `DisplayConfigGetDeviceInfo`
//! (`DISPLAYCONFIG_SDR_WHITE_LEVEL`), captures linear scRGB `RGBA16F` frames via free-threaded
//! WGC (`Direct3D11CaptureFramePool::CreateFreeThreaded`), and tonemaps only the requested ROI
//! to SDR `Bgra8`/`Bgr8` via [`crate::hdr_tonemap`].
//!
//! Strictly read-only — zero `SendInput`, zero external DLLs beyond standard Windows OS libraries.

use crate::hdr_tonemap::{
    DEFAULT_SDR_WHITE_LEVEL_NITS, sdr_white_level_from_windows_raw, tonemap_rgba16f_to_bgra8,
};
use gh_core::frame::{FrameBuf, PixelFormat};
use std::collections::HashMap;
use std::ffi::c_void;
use std::mem::size_of;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};
use windows::Graphics::Capture::{
    Direct3D11CaptureFramePool, GraphicsCaptureItem, GraphicsCaptureSession,
};
use windows::Graphics::DirectX::Direct3D11::IDirect3DDevice;
use windows::Graphics::DirectX::DirectXPixelFormat;
use windows::Graphics::SizeInt32;
use windows::Win32::Devices::Display::{
    DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL, DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
    DISPLAYCONFIG_DEVICE_INFO_HEADER, DISPLAYCONFIG_MODE_INFO, DISPLAYCONFIG_PATH_INFO,
    DISPLAYCONFIG_SDR_WHITE_LEVEL, DISPLAYCONFIG_SOURCE_DEVICE_NAME, DisplayConfigGetDeviceInfo,
    GetDisplayConfigBufferSizes, QDC_ONLY_ACTIVE_PATHS, QueryDisplayConfig,
};
use windows::Win32::Foundation::{ERROR_SUCCESS, HMODULE, POINT, RECT, RPC_E_CHANGED_MODE, S_OK};
use windows::Win32::Graphics::Direct3D::{
    D3D_DRIVER_TYPE_HARDWARE, D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_11_1,
};
use windows::Win32::Graphics::Direct3D11::{
    D3D11_BOX, D3D11_CPU_ACCESS_READ, D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_MAP_READ,
    D3D11_MAPPED_SUBRESOURCE, D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC, D3D11_USAGE_DEFAULT,
    D3D11_USAGE_STAGING, D3D11CreateDevice, ID3D11Device, ID3D11DeviceContext, ID3D11Resource,
    ID3D11Texture2D,
};
use windows::Win32::Graphics::Dxgi::Common::{
    DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020, DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_SAMPLE_DESC,
};
use windows::Win32::Graphics::Dxgi::{
    CreateDXGIFactory1, IDXGIDevice, IDXGIFactory1, IDXGIOutput6,
};
use windows::Win32::Graphics::Gdi::{HMONITOR, MONITOR_DEFAULTTONULL, MonitorFromPoint};
use windows::Win32::System::WinRT::Direct3D11::{
    CreateDirect3D11DeviceFromDXGIDevice, IDirect3DDxgiInterfaceAccess,
};
use windows::Win32::System::WinRT::Graphics::Capture::IGraphicsCaptureItemInterop;
use windows::Win32::System::WinRT::{RO_INIT_MULTITHREADED, RoInitialize, RoUninitialize};
use windows::core::Interface;

/// Refresh interval for cached DXGI monitor HDR status + Windows SDR white level.
const HDR_TOPOLOGY_TTL: Duration = Duration::from_secs(2);
/// Cooldown before retrying WGC initialization after a transient failure (e.g. GPU reset).
const WGC_RETRY_COOLDOWN: Duration = Duration::from_secs(3);
/// Max wait for the very first DWM frame after starting a new WGC monitor capture session.
const WGC_FIRST_FRAME_WAIT: Duration = Duration::from_millis(160);
/// Cached staging texture slots per WGC monitor session (matches GDI's 4-slot DIB pool).
const WGC_STAGING_POOL_SLOTS: usize = 4;

/// HDR capability and desktop coordinates for a single Windows display monitor.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MonitorHdrInfo {
    /// Raw `HMONITOR` handle stored as `isize`.
    pub hmonitor: isize,
    /// Monitor desktop rectangle `(left, top, width, height)` in virtual desktop pixels.
    pub rect: (i32, i32, i32, i32),
    /// True when Windows Advanced Color / HDR10 (`G2084_P2020`) is active on this monitor.
    pub is_hdr: bool,
    /// Windows "SDR content brightness" white level in nits (`[80.0, 1000.0]`).
    pub sdr_white_nits: f32,
}

impl MonitorHdrInfo {
    /// Returns true if screen region `(left, top, width, height)` lies wholly inside this monitor.
    #[inline]
    pub fn contains_region(&self, left: i32, top: i32, width: i32, height: i32) -> bool {
        if width <= 0 || height <= 0 {
            return false;
        }
        let (ml, mt, mw, mh) = self.rect;
        if mw <= 0 || mh <= 0 {
            return false;
        }
        let r_right = i64::from(left) + i64::from(width);
        let r_bottom = i64::from(top) + i64::from(height);
        let m_right = i64::from(ml) + i64::from(mw);
        let m_bottom = i64::from(mt) + i64::from(mh);
        i64::from(left) >= i64::from(ml)
            && i64::from(top) >= i64::from(mt)
            && r_right <= m_right
            && r_bottom <= m_bottom
    }
}

struct WinRtThreadGuard {
    needs_uninit: bool,
}

impl Drop for WinRtThreadGuard {
    fn drop(&mut self) {
        if self.needs_uninit {
            // SAFETY: Paired 1:1 with a successful `RoInitialize(RO_INIT_MULTITHREADED)` on this thread.
            unsafe {
                RoUninitialize();
            }
        }
    }
}

thread_local! {
    static WINRT_THREAD_INIT: std::cell::RefCell<Option<WinRtThreadGuard>> =
        const { std::cell::RefCell::new(None) };
}

fn ensure_winrt_initialized() {
    let _ = WINRT_THREAD_INIT.try_with(|cell| {
        let mut slot = cell.borrow_mut();
        if slot.is_some() {
            return;
        }
        // SAFETY: Initializes WinRT MTA for the calling thread; `S_FALSE` or `RPC_E_CHANGED_MODE`
        // means the thread is already initialized and must not be uninitialized by us.
        let hr = unsafe { RoInitialize(RO_INIT_MULTITHREADED) };
        let needs_uninit = match hr {
            Ok(()) => true,
            Err(e) if e.code() == S_OK => true,
            Err(e) if e.code() == RPC_E_CHANGED_MODE => false,
            Err(_) => false,
        };
        *slot = Some(WinRtThreadGuard { needs_uninit });
    });
}

fn utf16_cstr_to_string(buf: &[u16]) -> String {
    let end = buf.iter().position(|&c| c == 0).unwrap_or(buf.len());
    String::from_utf16_lossy(&buf[..end])
}

/// Queries active DisplayConfig paths and maps GDI device names (e.g. `\\.\DISPLAY1`) to
/// their Windows SDR content brightness white level in nits.
fn query_sdr_white_levels_by_device_name() -> HashMap<String, f32> {
    let mut out = HashMap::new();
    let mut num_paths = 0u32;
    let mut num_modes = 0u32;
    // SAFETY: Querying buffer sizes for active display paths.
    let err = unsafe {
        GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &mut num_paths, &mut num_modes)
    };
    if err != ERROR_SUCCESS || num_paths == 0 {
        return out;
    }

    let mut paths = vec![DISPLAYCONFIG_PATH_INFO::default(); num_paths as usize];
    let mut modes = vec![DISPLAYCONFIG_MODE_INFO::default(); num_modes as usize];
    // SAFETY: `paths` and `modes` are sized according to `GetDisplayConfigBufferSizes`.
    let err = unsafe {
        QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS,
            &mut num_paths,
            paths.as_mut_ptr(),
            &mut num_modes,
            modes.as_mut_ptr(),
            None,
        )
    };
    if err != ERROR_SUCCESS {
        return out;
    }

    for path in paths.iter().take(num_paths as usize) {
        let mut src_name = DISPLAYCONFIG_SOURCE_DEVICE_NAME {
            header: DISPLAYCONFIG_DEVICE_INFO_HEADER {
                r#type: DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
                size: size_of::<DISPLAYCONFIG_SOURCE_DEVICE_NAME>() as u32,
                adapterId: path.sourceInfo.adapterId,
                id: path.sourceInfo.id,
            },
            ..Default::default()
        };
        // SAFETY: `src_name.header` is initialized with exact struct size and type tag.
        let rc_src = unsafe { DisplayConfigGetDeviceInfo(&mut src_name.header) };
        if rc_src != ERROR_SUCCESS.0 as i32 {
            continue;
        }
        let dev_name = utf16_cstr_to_string(&src_name.viewGdiDeviceName);
        if dev_name.is_empty() {
            continue;
        }

        let mut sdr_info = DISPLAYCONFIG_SDR_WHITE_LEVEL {
            header: DISPLAYCONFIG_DEVICE_INFO_HEADER {
                r#type: DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL,
                size: size_of::<DISPLAYCONFIG_SDR_WHITE_LEVEL>() as u32,
                adapterId: path.targetInfo.adapterId,
                id: path.targetInfo.id,
            },
            SDRWhiteLevel: 0,
        };
        // SAFETY: `sdr_info.header` is initialized with exact struct size and type tag.
        let rc_sdr = unsafe { DisplayConfigGetDeviceInfo(&mut sdr_info.header) };
        let nits = if rc_sdr == ERROR_SUCCESS.0 as i32 && sdr_info.SDRWhiteLevel > 0 {
            sdr_white_level_from_windows_raw(sdr_info.SDRWhiteLevel)
        } else {
            DEFAULT_SDR_WHITE_LEVEL_NITS
        };
        out.insert(dev_name.to_ascii_uppercase(), nits);
    }
    out
}

/// Enumerates all active DXGI outputs and returns their HDR state, desktop rects, and SDR white levels.
pub fn enumerate_monitors_hdr_info() -> Vec<MonitorHdrInfo> {
    let mut monitors = Vec::new();
    // SAFETY: Standard DXGI 1.1 factory enumeration (read-only).
    let Ok(factory) = (unsafe { CreateDXGIFactory1::<IDXGIFactory1>() }) else {
        return monitors;
    };
    let sdr_levels = query_sdr_white_levels_by_device_name();

    let mut adapter_idx = 0u32;
    loop {
        // SAFETY: Enumerates adapters until `DXGI_ERROR_NOT_FOUND`.
        let Ok(adapter) = (unsafe { factory.EnumAdapters1(adapter_idx) }) else {
            break;
        };
        adapter_idx += 1;

        let mut output_idx = 0u32;
        loop {
            // SAFETY: Enumerates outputs on `adapter` until `DXGI_ERROR_NOT_FOUND`.
            let Ok(output) = (unsafe { adapter.EnumOutputs(output_idx) }) else {
                break;
            };
            output_idx += 1;

            let Ok(output6) = output.cast::<IDXGIOutput6>() else {
                continue;
            };
            // SAFETY: Queries `DXGI_OUTPUT_DESC1` on valid `IDXGIOutput6`.
            let Ok(desc) = (unsafe { output6.GetDesc1() }) else {
                continue;
            };
            if !desc.AttachedToDesktop.as_bool() || desc.Monitor.is_invalid() {
                continue;
            }
            let r: RECT = desc.DesktopCoordinates;
            let w = r.right.saturating_sub(r.left);
            let h = r.bottom.saturating_sub(r.top);
            if w <= 0 || h <= 0 {
                continue;
            }
            let dev_name = utf16_cstr_to_string(&desc.DeviceName).to_ascii_uppercase();
            let sdr_white_nits = sdr_levels
                .get(&dev_name)
                .copied()
                .unwrap_or(DEFAULT_SDR_WHITE_LEVEL_NITS);
            let is_hdr = desc.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;

            monitors.push(MonitorHdrInfo {
                hmonitor: desc.Monitor.0 as isize,
                rect: (r.left, r.top, w, h),
                is_hdr,
                sdr_white_nits,
            });
        }
    }
    monitors
}

struct CachedHdrTopology {
    last_refresh: Instant,
    monitors: HashMap<isize, MonitorHdrInfo>,
    any_hdr: bool,
}

static HDR_TOPOLOGY_CACHE: OnceLock<Mutex<Option<CachedHdrTopology>>> = OnceLock::new();

fn with_cached_hdr_topology<R>(f: impl FnOnce(&CachedHdrTopology) -> R) -> R {
    let cache = HDR_TOPOLOGY_CACHE.get_or_init(|| Mutex::new(None));
    let mut guard = cache.lock().unwrap_or_else(|e| e.into_inner());
    let needs_refresh = match guard.as_ref() {
        None => true,
        Some(c) => c.last_refresh.elapsed() >= HDR_TOPOLOGY_TTL,
    };
    if needs_refresh {
        let list = enumerate_monitors_hdr_info();
        let any_hdr = list.iter().any(|m| m.is_hdr);
        let mut map = HashMap::with_capacity(list.len());
        for m in list {
            map.insert(m.hmonitor, m);
        }
        *guard = Some(CachedHdrTopology {
            last_refresh: Instant::now(),
            monitors: map,
            any_hdr,
        });
    }
    f(guard.as_ref().expect("topology cache initialized"))
}

/// Forces the cached DXGI monitor HDR topology to refresh on the next probe.
pub fn invalidate_hdr_topology_cache() {
    if let Some(cache) = HDR_TOPOLOGY_CACHE.get()
        && let Ok(mut guard) = cache.lock()
    {
        *guard = None;
    }
}

/// Returns [`MonitorHdrInfo`] if the requested screen region lies wholly inside an active HDR monitor.
///
/// Fast-paths in `< 1 µs` on SDR-only desktops without touching D3D11 or WinRT.
pub fn probe_hdr_monitor_for_region(
    left: i32,
    top: i32,
    width: i32,
    height: i32,
) -> Option<MonitorHdrInfo> {
    if width <= 0 || height <= 0 {
        return None;
    }
    with_cached_hdr_topology(|topo| {
        if !topo.any_hdr {
            return None;
        }
        let cx = left.saturating_add(width / 2);
        let cy = top.saturating_add(height / 2);
        // SAFETY: `MonitorFromPoint` is a read-only Win32 query.
        let hmon = unsafe { MonitorFromPoint(POINT { x: cx, y: cy }, MONITOR_DEFAULTTONULL) };
        if hmon.is_invalid() {
            return None;
        }
        let info = topo.monitors.get(&(hmon.0 as isize)).copied()?;
        if info.is_hdr && info.contains_region(left, top, width, height) {
            Some(info)
        } else {
            None
        }
    })
}

/// Returns true if Windows Graphics Capture is supported on the current Windows version.
pub fn is_wgc_supported() -> bool {
    ensure_winrt_initialized();
    GraphicsCaptureSession::IsSupported().unwrap_or(false)
}

struct StagingSlot {
    width: u32,
    height: u32,
    tex: ID3D11Texture2D,
}

/// Persistent free-threaded WGC `R16G16B16A16Float` capture session bound to one `HMONITOR`.
pub struct WgcHdrMonitorSession {
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    winrt_device: IDirect3DDevice,
    frame_pool: Direct3D11CaptureFramePool,
    session: GraphicsCaptureSession,
    content_size: SizeInt32,
    latest_gpu_tex: Option<ID3D11Texture2D>,
    latest_tex_dims: (u32, u32),
    staging_slots: Vec<StagingSlot>,
    tonemap_bgra: Vec<u8>,
    closed: bool,
}

// SAFETY: All WinRT/D3D11 interfaces are initialized in MTA and exclusively accessed while
// holding the parent `Mutex` (single-owner serialized immediate context access).
unsafe impl Send for WgcHdrMonitorSession {}

struct MappedTextureGuard<'a> {
    context: &'a ID3D11DeviceContext,
    resource: &'a ID3D11Resource,
}

impl Drop for MappedTextureGuard<'_> {
    fn drop(&mut self) {
        // SAFETY: Paired 1:1 with a successful `ID3D11DeviceContext::Map`.
        unsafe {
            self.context.Unmap(self.resource, 0);
        }
    }
}

impl WgcHdrMonitorSession {
    /// Opens a live WGC `R16G16B16A16Float` capture session on `hmonitor`.
    pub fn try_open_monitor(hmonitor: isize) -> Option<Self> {
        if hmonitor == 0 || !is_wgc_supported() {
            return None;
        }
        ensure_winrt_initialized();

        let feature_levels = [D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0];
        let mut device: Option<ID3D11Device> = None;
        let mut context: Option<ID3D11DeviceContext> = None;
        // SAFETY: Creating a standard hardware D3D11 device with BGRA support for WGC interop.
        unsafe {
            D3D11CreateDevice(
                None,
                D3D_DRIVER_TYPE_HARDWARE,
                HMODULE::default(),
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                Some(&feature_levels),
                D3D11_SDK_VERSION,
                Some(&mut device),
                None,
                Some(&mut context),
            )
            .ok()?;
        }
        let device = device?;
        let context = context?;
        let dxgi_device: IDXGIDevice = device.cast().ok()?;
        // SAFETY: Wrapping valid `IDXGIDevice` into WinRT `IDirect3DDevice`.
        let inspectable = unsafe { CreateDirect3D11DeviceFromDXGIDevice(&dxgi_device).ok()? };
        let winrt_device: IDirect3DDevice = inspectable.cast().ok()?;

        let interop =
            windows::core::factory::<GraphicsCaptureItem, IGraphicsCaptureItemInterop>().ok()?;
        // SAFETY: Creating capture item for validated non-null `HMONITOR`.
        let item: GraphicsCaptureItem =
            unsafe { interop.CreateForMonitor(HMONITOR(hmonitor as *mut c_void)).ok()? };
        let content_size = item.Size().ok()?;
        if content_size.Width <= 0 || content_size.Height <= 0 {
            return None;
        }

        let frame_pool = Direct3D11CaptureFramePool::CreateFreeThreaded(
            &winrt_device,
            DirectXPixelFormat::R16G16B16A16Float,
            2,
            content_size,
        )
        .ok()?;
        let session = frame_pool.CreateCaptureSession(&item).ok()?;
        // Disable cursor capture (matches GDI SRCCOPY) and border glow (best-effort on Win11).
        let _ = session.SetIsCursorCaptureEnabled(false);
        let _ = session.SetIsBorderRequired(false);
        session.StartCapture().ok()?;

        Some(Self {
            device,
            context,
            winrt_device,
            frame_pool,
            session,
            content_size,
            latest_gpu_tex: None,
            latest_tex_dims: (0, 0),
            staging_slots: Vec::with_capacity(WGC_STAGING_POOL_SLOTS),
            tonemap_bgra: Vec::new(),
            closed: false,
        })
    }

    fn ensure_latest_gpu_tex(&mut self, width: u32, height: u32) -> Option<()> {
        if self.latest_gpu_tex.is_some() && self.latest_tex_dims == (width, height) {
            return Some(());
        }
        let desc = D3D11_TEXTURE2D_DESC {
            Width: width,
            Height: height,
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_R16G16B16A16_FLOAT,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DEFAULT,
            BindFlags: 0,
            CPUAccessFlags: 0,
            MiscFlags: 0,
        };
        let mut tex: Option<ID3D11Texture2D> = None;
        // SAFETY: Creating GPU-only copy target matching WGC `R16G16B16A16Float` surface.
        unsafe {
            self.device
                .CreateTexture2D(&desc, None, Some(&mut tex))
                .ok()?;
        }
        self.latest_gpu_tex = tex;
        self.latest_tex_dims = (width, height);
        Some(())
    }

    fn get_or_create_staging(&mut self, width: u32, height: u32) -> Option<ID3D11Texture2D> {
        if let Some(pos) = self
            .staging_slots
            .iter()
            .position(|s| s.width == width && s.height == height)
        {
            let entry = self.staging_slots.remove(pos);
            let tex = entry.tex.clone();
            self.staging_slots.push(entry);
            return Some(tex);
        }
        while self.staging_slots.len() >= WGC_STAGING_POOL_SLOTS {
            self.staging_slots.remove(0);
        }
        let desc = D3D11_TEXTURE2D_DESC {
            Width: width,
            Height: height,
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_R16G16B16A16_FLOAT,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_STAGING,
            BindFlags: 0,
            CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
            MiscFlags: 0,
        };
        let mut tex: Option<ID3D11Texture2D> = None;
        // SAFETY: Creating CPU-readable staging texture for ROI readback.
        unsafe {
            self.device
                .CreateTexture2D(&desc, None, Some(&mut tex))
                .ok()?;
        }
        let tex = tex?;
        self.staging_slots.push(StagingSlot {
            width,
            height,
            tex: tex.clone(),
        });
        Some(tex)
    }

    /// Drains all queued WGC frames and copies the newest `RGBA16F` frame into `latest_gpu_tex`.
    fn drain_latest_frame(&mut self) -> Result<bool, ()> {
        let mut newest = None;
        let mut resized_to: Option<SizeInt32> = None;
        while let Ok(frame) = self.frame_pool.TryGetNextFrame() {
            if let Ok(sz) = frame.ContentSize()
                && sz.Width > 0
                && sz.Height > 0
                && (sz.Width != self.content_size.Width || sz.Height != self.content_size.Height)
            {
                resized_to = Some(sz);
            }
            if let Some(prev) = newest.replace(frame) {
                let _ = prev.Close();
            }
        }

        let Some(frame) = newest else {
            return Ok(false);
        };

        let copy_res = (|| -> Result<(), ()> {
            let surface = frame.Surface().map_err(|_| ())?;
            let access: IDirect3DDxgiInterfaceAccess = surface.cast().map_err(|_| ())?;
            // SAFETY: Extracting `ID3D11Texture2D` backing the WGC frame surface.
            let src_tex: ID3D11Texture2D = unsafe { access.GetInterface().map_err(|_| ())? };
            let mut desc = D3D11_TEXTURE2D_DESC::default();
            // SAFETY: Querying texture descriptor on valid `ID3D11Texture2D`.
            unsafe { src_tex.GetDesc(&mut desc) };
            if desc.Format != DXGI_FORMAT_R16G16B16A16_FLOAT || desc.Width == 0 || desc.Height == 0
            {
                return Err(());
            }
            self.ensure_latest_gpu_tex(desc.Width, desc.Height).ok_or(())?;
            let dst_tex = self.latest_gpu_tex.as_ref().ok_or(())?;
            let dst_res: ID3D11Resource = dst_tex.cast().map_err(|_| ())?;
            let src_res: ID3D11Resource = src_tex.cast().map_err(|_| ())?;
            // SAFETY: Both textures share identical dimensions and `R16G16B16A16_FLOAT` format.
            unsafe {
                self.context.CopyResource(&dst_res, &src_res);
            }
            Ok(())
        })();

        let _ = frame.Close();

        if let Some(new_sz) = resized_to {
            self.content_size = new_sz;
            let _ = self.frame_pool.Recreate(
                &self.winrt_device,
                DirectXPixelFormat::R16G16B16A16Float,
                2,
                new_sz,
            );
        }

        copy_res.map(|()| true)
    }

    /// Captures monitor-relative ROI `(rx, ry, rw, rh)` from the latest WGC `RGBA16F` surface
    /// and tonemaps it to SDR `Bgra8` (or `Bgr8` when `bgra == false`).
    pub fn capture_roi(
        &mut self,
        rx: u32,
        ry: u32,
        rw: u32,
        rh: u32,
        sdr_white_nits: f32,
        bgra: bool,
    ) -> Option<FrameBuf> {
        if self.closed || rw == 0 || rh == 0 {
            return None;
        }
        if self.drain_latest_frame().is_err() {
            return None;
        }
        if self.latest_gpu_tex.is_none() {
            let deadline = Instant::now() + WGC_FIRST_FRAME_WAIT;
            while self.latest_gpu_tex.is_none() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(4));
                if self.drain_latest_frame().is_err() {
                    return None;
                }
            }
        }
        let (tex_w, tex_h) = self.latest_tex_dims;
        if rx.checked_add(rw)? > tex_w || ry.checked_add(rh)? > tex_h {
            return None;
        }

        let staging = self.get_or_create_staging(rw, rh)?;
        let gpu_tex = self.latest_gpu_tex.as_ref()?;
        let dst_res: ID3D11Resource = staging.cast().ok()?;
        let src_res: ID3D11Resource = gpu_tex.cast().ok()?;

        let src_box = D3D11_BOX {
            left: rx,
            top: ry,
            front: 0,
            right: rx + rw,
            bottom: ry + rh,
            back: 1,
        };
        // SAFETY: `src_box` is strictly within `[0..tex_w, 0..tex_h]` and matches staging `(rw, rh)`.
        unsafe {
            self.context
                .CopySubresourceRegion(&dst_res, 0, 0, 0, 0, &src_res, 0, Some(&src_box));
        }

        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        // SAFETY: Mapping staging texture for synchronous CPU readback.
        unsafe {
            self.context
                .Map(&dst_res, 0, D3D11_MAP_READ, 0, Some(&mut mapped))
                .ok()?;
        }
        let _unmap_guard = MappedTextureGuard {
            context: &self.context,
            resource: &dst_res,
        };

        if mapped.pData.is_null() {
            return None;
        }
        let row_pitch = mapped.RowPitch as usize;
        let min_row_bytes = (rw as usize).checked_mul(8)?;
        if row_pitch < min_row_bytes {
            return None;
        }
        let total_src_bytes = (rh as usize)
            .saturating_sub(1)
            .checked_mul(row_pitch)?
            .checked_add(min_row_bytes)?;
        // SAFETY: `mapped.pData` points to at least `total_src_bytes` readable staging bytes
        // until `_unmap_guard` calls `Unmap`.
        let src_bytes =
            unsafe { std::slice::from_raw_parts(mapped.pData.cast::<u8>(), total_src_bytes) };

        tonemap_rgba16f_to_bgra8(
            src_bytes,
            rw,
            rh,
            row_pitch,
            sdr_white_nits,
            &mut self.tonemap_bgra,
        )
        .ok()?;
        drop(_unmap_guard);

        if bgra {
            let mut out = FrameBuf::new_zeroed(rw, rh, PixelFormat::Bgra8);
            out.data.copy_from_slice(&self.tonemap_bgra);
            Some(out)
        } else {
            let mut out = FrameBuf::new_zeroed(rw, rh, PixelFormat::Bgr8);
            for (src_px, dst_px) in self
                .tonemap_bgra
                .as_chunks::<4>()
                .0
                .iter()
                .zip(out.data.as_chunks_mut::<3>().0.iter_mut())
            {
                dst_px[0] = src_px[0];
                dst_px[1] = src_px[1];
                dst_px[2] = src_px[2];
            }
            Some(out)
        }
    }

    pub fn close(&mut self) {
        if self.closed {
            return;
        }
        self.closed = true;
        self.staging_slots.clear();
        self.latest_gpu_tex = None;
        let _ = self.session.Close();
        let _ = self.frame_pool.Close();
    }
}

impl Drop for WgcHdrMonitorSession {
    fn drop(&mut self) {
        self.close();
    }
}

struct WgcMonitorEntry {
    session: Option<WgcHdrMonitorSession>,
    retry_after: Option<Instant>,
}

/// Manages lazy per-monitor WGC HDR capture sessions with automatic GDI fallback cooldown.
pub struct WgcHdrManager {
    entries: Mutex<HashMap<isize, WgcMonitorEntry>>,
}

impl Default for WgcHdrManager {
    fn default() -> Self {
        Self::new()
    }
}

impl WgcHdrManager {
    pub fn new() -> Self {
        Self {
            entries: Mutex::new(HashMap::new()),
        }
    }

    /// Attempts HDR WGC capture if `(left, top, width, height)` sits wholly inside an active HDR
    /// monitor. Returns `None` on SDR monitors or transient WGC errors so caller falls back to GDI.
    pub fn try_capture_hdr_region(
        &self,
        left: i32,
        top: i32,
        width: i32,
        height: i32,
        bgra: bool,
    ) -> Option<FrameBuf> {
        let info = probe_hdr_monitor_for_region(left, top, width, height)?;
        let rx = u32::try_from(left - info.rect.0).ok()?;
        let ry = u32::try_from(top - info.rect.1).ok()?;
        let rw = u32::try_from(width).ok()?;
        let rh = u32::try_from(height).ok()?;

        let mut map = self.entries.lock().ok()?;
        let entry = map
            .entry(info.hmonitor)
            .or_insert_with(|| WgcMonitorEntry {
                session: None,
                retry_after: None,
            });
        if entry.session.is_none() {
            if entry
                .retry_after
                .is_some_and(|until| Instant::now() < until)
            {
                return None;
            }
            match WgcHdrMonitorSession::try_open_monitor(info.hmonitor) {
                Some(sess) => {
                    entry.session = Some(sess);
                    entry.retry_after = None;
                }
                None => {
                    entry.retry_after = Some(Instant::now() + WGC_RETRY_COOLDOWN);
                    return None;
                }
            }
        }

        let captured = entry
            .session
            .as_mut()
            .and_then(|s| s.capture_roi(rx, ry, rw, rh, info.sdr_white_nits, bgra));
        if captured.is_some() {
            return captured;
        }

        // Device reset or monitor disconnect: tear down and cool down so GDI takes over cleanly.
        if let Some(mut sess) = entry.session.take() {
            sess.close();
        }
        entry.retry_after = Some(Instant::now() + WGC_RETRY_COOLDOWN);
        None
    }

    pub fn close(&self) {
        if let Ok(mut map) = self.entries.lock() {
            for (_, mut entry) in map.drain() {
                if let Some(mut sess) = entry.session.take() {
                    sess.close();
                }
            }
        }
    }
}

impl Drop for WgcHdrManager {
    fn drop(&mut self) {
        self.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn monitor_hdr_info_region_containment_handles_bounds_and_overflow() {
        let mon = MonitorHdrInfo {
            hmonitor: 1,
            rect: (1920, 0, 2560, 1440),
            is_hdr: true,
            sdr_white_nits: 240.0,
        };
        assert!(mon.contains_region(1920, 0, 2560, 1440));
        assert!(mon.contains_region(2000, 100, 218, 218));
        assert!(!mon.contains_region(1919, 0, 100, 100));
        assert!(!mon.contains_region(1920, 0, 2561, 1440));
        assert!(!mon.contains_region(1920, 0, 0, 100));
        assert!(!mon.contains_region(i32::MAX, 0, 10, 10));
    }

    #[test]
    fn enumerate_monitors_hdr_info_live_smoke_returns_valid_geometry_and_nits() {
        let monitors = enumerate_monitors_hdr_info();
        for m in &monitors {
            assert_ne!(m.hmonitor, 0);
            assert!(m.rect.2 > 0 && m.rect.3 > 0);
            assert!(
                m.sdr_white_nits.is_finite()
                    && (80.0..=1000.0).contains(&m.sdr_white_nits),
                "unexpected sdr_white_nits={}",
                m.sdr_white_nits
            );
        }
    }

    #[test]
    fn wgc_rgba16f_monitor_session_live_roundtrip_when_supported() {
        if !is_wgc_supported() {
            return;
        }
        let monitors = enumerate_monitors_hdr_info();
        let Some(mon) = monitors.first().copied() else {
            return;
        };
        let Some(mut sess) = WgcHdrMonitorSession::try_open_monitor(mon.hmonitor) else {
            // Headless / CI / RDP desktop without DWM hardware WGC: skip gracefully.
            return;
        };
        let rw = (mon.rect.2 as u32).min(128);
        let rh = (mon.rect.3 as u32).min(96);
        let bgra = sess.capture_roi(0, 0, rw, rh, mon.sdr_white_nits, true);
        let bgr = sess.capture_roi(0, 0, rw, rh, mon.sdr_white_nits, false);
        sess.close();
        if let Some(f) = bgra {
            assert_eq!((f.width, f.height, f.format), (rw, rh, PixelFormat::Bgra8));
            assert_eq!(f.data.len(), (rw * rh * 4) as usize);
            assert!(f.data.as_chunks::<4>().0.iter().all(|px| px[3] == 255));
        }
        if let Some(f) = bgr {
            assert_eq!((f.width, f.height, f.format), (rw, rh, PixelFormat::Bgr8));
            assert_eq!(f.data.len(), (rw * rh * 3) as usize);
        }
    }
}
