<div align="center">
  <img src="assets/icons/app_compass_origin.png" alt="Biểu tượng La bàn Genshin-Helper" width="96" height="96" />
  <h1>Genshin-Helper v5.3</h1>
  <p><strong>Cầu nối Thị giác Máy tính Thụ động &amp; Điều hướng 360° cho HoYoLAB Interactive Map</strong></p>
  <p>
    <a href="README.md">English</a> | <strong>Tiếng Việt</strong>
  </p>
</div>

---

## Tổng quan

**Genshin-Helper** là công cụ hỗ trợ chạy hoàn toàn cục bộ trên Windows 10/11 (64-bit), sử dụng **Thị giác Máy tính (Computer Vision) thụ động 100%** để đồng bộ vị trí từ **Genshin Impact** sang **Bản đồ tương tác HoYoLAB** (qua tiện ích mở rộng Chrome / Edge / Brave MV3) — tuyệt đối không đọc RAM game, không chèn DLL và không gửi phím/chuột giả vào game.

### Tính năng nổi bật

- **Định vị Map (`M`) chính xác & Theo dõi Minimap lai (Hybrid V5.3):** Hỗ trợ đa bản đồ (Teyvat, Enkanomiya, Vực Đá Sâu, Biển Kỷ Nguyên Cũ...), tinh chỉnh địa hình Sobel, nhận diện tầng hang ngầm và máy trạng thái theo dõi minimap 4 bước (`LOST` / `ACQUIRING` / `TRACKING` / `RELOCATING`), bổ sung SIFT trên tile HD + Sobel chuẩn hóa liên khung, tự động chuyển tầng mặt đất ↔ hang ngầm, tự hiệu chỉnh lệch HUD khi chạy và tự tìm lại vị trí toàn Teyvat bằng RootSIFT offline.
- **Mini-HUD đồng bộ góc nhìn Camera 360° & Thẻ gợi ý địa điểm cận kề:** Lớp phủ trong suốt xuyên chuột (`UpdateLayeredWindow` + `WDA_EXCLUDEFROMCAPTURE`) hiển thị hướng nhìn 360° thực, khóa mục tiêu thông minh (sticky target), cơ chế giữ để xác nhận đã nhặt (tránh bấm nhầm) và thẻ mẹo khi đứng gần ghim.
- **Lập tuyến đường farm (TSP Route Planner) & Xem phụ trên mạng LAN:** Tối ưu lộ trình ngắn nhất hiển thị rõ hướng đi ngay trên bản đồ, kèm chế độ xem chỉ đọc qua mạng LAN trên điện thoại/máy tính bảng.
- **Xuất ghim liên bản đồ (Cross-Map Pin Exporter):** Trích xuất thụ động các ghim đã đánh dấu trên HoYoLAB ngay trong phiên trình duyệt để chuyển sang các bản đồ mã nguồn mở (Kongying Tavern, `genshinmap.github.io`, Appsample) có hiệu chỉnh tọa độ và sao lưu an toàn.
- **Trung tâm điều khiển Extension MV3 & Chế độ 1 Màn hình:** Bề mặt bản đồ sạch 100%, huy hiệu trạng thái `ON` / `ZZZ` (nghỉ 0% CPU) / `OFF`, tự đánh thức qua Native Messaging Host, chuyển nhanh Game ↔ Map (`Alt+Q`), đồng bộ & ghim gần nhất (`Alt+F11`), và chụp gói chẩn đoán (`Alt+F12` — tự động làm mờ UID trong ảnh).
- **Song ngữ Việt / Anh & Tự động tải cache cục bộ:** Dữ liệu bản đồ được tải về thư mục cache người dùng `%LOCALAPPDATA%\GenshinHelper\data` với cơ chế nâng cấp nguyên tử (atomic rollback) và kiểm tra MD5 (`src/asset_setup.py`).

---

## An toàn, Chống gian lận (Anti-Cheat) & Miễn trừ trách nhiệm

- **Thị giác Máy tính thụ động 100%:** Chỉ đọc điểm ảnh màn hình qua API chuẩn của Windows (DXGI/BetterCam, MSS, BitBlt) và dữ liệu bản đồ công khai của HoYoLAB.
- **Không can thiệp game:** Không bao giờ đọc/ghi bộ nhớ (RAM) của game, không inject DLL, không hook tiến trình game, và **không bao giờ dùng `SendInput`** để gửi thao tác phím/chuột vào cửa sổ game.
- **Riêng tư cục bộ:** Máy chủ đồng bộ chỉ lắng nghe trên `127.0.0.1:8765`. Không thu thập dữ liệu đo từ xa (telemetry) và không lưu hay gửi cookie đăng nhập HoYoLAB ra ngoài.
- **Lưu ý Điều khoản dịch vụ (ToS):** Genshin-Helper là dự án cộng đồng không chính thức, không liên kết hay được bảo trợ bởi HoYoverse / COGNOSPHERE PTE. LTD. Dù được thiết kế hoàn toàn chỉ đọc và thụ động, người dùng tự chịu trách nhiệm khi sử dụng mọi phần mềm hỗ trợ bên thứ ba. Xem thêm [SECURITY.md](SECURITY.md) và [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

## Bảng tương thích cài đặt game & Giới hạn hệ điều hành

Genshin-Helper chuẩn hóa tọa độ giao diện theo quy luật Unity `CanvasScaler` và xử lý minh bạch **3 giới hạn cứng của Windows / game**:

| Cài đặt / Môi trường | Mức hỗ trợ | Hành vi & Khuyến nghị |
| --- | --- | --- |
| **Cửa sổ không viền (Borderless) / Cửa sổ (Windowed)** | Hỗ trợ đầy đủ | Khuyên dùng để hiển thị Mini-HUD, thông báo HUD Toast và dùng trên 1 màn hình. |
| **Toàn màn hình độc quyền (Exclusive Fullscreen)** *(Giới hạn cứng OS)* | Hạn chế (không hiện lớp phủ) | Windows DWM chặn cửa sổ lớp phủ hiển thị đè lên Exclusive Fullscreen. Hãy dùng nút **Khởi chạy Borderless 1-click** tích hợp (`-popupwindow -screen-fullscreen 0`) hoặc chuyển trong cài đặt game. |
| **Quyền Quản trị & UIPI** *(Giới hạn cứng Windows)* | Cần cùng mức quyền Admin | Genshin Impact chạy với quyền Administrator nên Windows UIPI chặn phím tắt toàn cục từ app thường. Hãy chạy Genshin-Helper bằng quyền Administrator hoặc cài qua **Bộ cài Setup** (tạo Scheduled Task nâng quyền không cần hỏi UAC). |
| **Windows Auto-HDR / HDR** *(Giới hạn cứng OS/Capture)* | Giảm độ chính xác / dùng nhánh SDR | Ánh xạ màu HDR/Auto-HDR làm lệch ngưỡng màu và mất kênh Alpha khi chụp màn hình. Hãy tắt **Auto-HDR** cho Genshin Impact trong cài đặt hiển thị Windows để nhận diện ổn định nhất. |
| **Tỷ lệ màn hình (`16:9`, `16:10`, `21:9`, `4:3`)** | Hỗ trợ đầy đủ | Tự động chuẩn hóa vùng minimap/HUD theo Unity `CanvasScaler` cho cả màn siêu rộng. |
| **Tỷ lệ phóng DPI Windows (`100%–200%`)** | Hỗ trợ đầy đủ | Xử lý DPI theo từng màn hình và tách hệ số X/Y chống lệch ở độ phân giải lẻ (ví dụ `1366×768` ở `125%`). |
| **Bàn phím/Chuột & Tay cầm (Controller/Gamepad)** | Hỗ trợ đầy đủ | Tự động nhận diện vùng an toàn (safe-area) minimap cho cả bố cục bàn phím lẫn tay cầm. |
| **Xoay Minimap (Khóa hướng Bắc & Xoay theo góc nhìn)** | Hỗ trợ đầy đủ | Thuật toán nón góc nhìn 360° và tách 3 đỉnh mũi tên hoạt động ở cả hai chế độ minimap. |

---

## Cài đặt

### Cách 1: Bộ cài Windows Setup (Khuyên dùng)

1. Tải và chạy `GenshinHelper-Setup-v*.exe` từ mục **Releases**.
2. Bộ cài tự động đăng ký **Native Messaging Host** (`com.genshinhelper.host`) cho Chrome, Edge, Brave và thiết lập tác vụ nâng quyền để Extension có thể bật tắt Desktop App không cần hộp thoại UAC.
3. Làm theo trang hướng dẫn đi kèm để cài đặt tiện ích mở rộng từ thư mục `extension/` (Extension ID cố định: `nfkijdoblmphmdljfogooajhbldkeojh`).

### Cách 2: Chạy từ mã nguồn (Windows 10/11 64-bit, Python 3.11+)

```powershell
git clone https://github.com/<owner>/Genshin-Helper.git
cd Genshin-Helper

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pillow

Copy-Item config.example.json config.json
```

Tải dữ liệu bản đồ HoYoLAB (địa hình, điểm dịch chuyển, lớp hang ngầm) về máy cục bộ:

```powershell
.\.venv\Scripts\python.exe scripts/sync_hoyolab_data.py
```

Cài đặt Browser Extension MV3 trên Chrome / Edge / Brave:
1. Mở `chrome://extensions` (hoặc `edge://extensions`) và bật **Developer mode** (Chế độ nhà phát triển).
2. Nhấn **Load unpacked** (Tải tiện ích đã giải nén) và chọn thư mục `extension/`.
3. Ghim biểu tượng **Genshin Helper** lên thanh công cụ và mở HoYoLAB Interactive Map.

Khởi chạy Desktop Engine (mở PowerShell bằng quyền Administrator nếu game chạy Admin):

```powershell
.\.venv\Scripts\python.exe main.py
```

---

## Kiến trúc hệ thống

```text
+----------------------------------+        localhost:8765 (HTTP/WS)        +---------------------------------+
|   Genshin-Helper Desktop (Win64) | <====================================> | Chrome / Edge / Brave Extension |
|                                  |  Ring buffer /poll?since=<seq> lossless|  (MV3 Service Worker + Popup)   |
|  - Chụp màn hình thụ động        |                                        |  - Cầu nối DOM HoYoLAB Map      |
|  - Bộ định vị Map M & Minimap    |        Native Messaging (stdio)        |  - Đồng bộ nền không giảm nhịp  |
|  - Góc nhìn 360° & Mini-HUD      | <------------------------------------> |  - Tuyến TSP & Xuất ghim mở     |
|  - Cache cục bộ (%LOCALAPPDATA%) |    com.genshinhelper.host (ID cố định) +---------------------------------+
+----------------------------------+
```

- **Desktop CV Engine (`src/`):** Chụp màn hình (`capture.py`), định vị đa bản đồ & hang ngầm (`locator.py`, `zoom_slider.py`, `map_state.py`), theo dõi minimap & góc 360°, phản hồi HUD/âm thanh 1 màn hình (`single_monitor.py`), quản lý cache nguyên tử (`asset_setup.py`) và chẩn đoán (`diagnostics.py`).
- **Máy chủ đồng bộ (`src/server.py`):** Máy chủ HTTP + WebSocket cục bộ dùng bộ đệm vòng đơn điệu `/poll?since=<seq>` giúp nhiều tab HoYoLAB đồng bộ mà không làm mất sự kiện dịch chuyển/đánh dấu.
- **Browser Extension (`extension/`):** Trung tâm điều khiển Manifest V3 gắn ID cố định (`nfkijdoblmphmdljfogooajhbldkeojh`), chạy ngầm không bị Chrome giảm nhịp khi tab bị game che khuất.

---

## Kiểm thử & Kiểm toán phát hành

```powershell
.\.venv\Scripts\python.exe tests/verify_v45_release.py
.\.venv\Scripts\python.exe tests/verify_single_monitor.py
.\.venv\Scripts\python.exe tests/verify_public_readiness_v5.py
.\.venv\Scripts\python.exe scripts/audit_release.py
.\.venv\Scripts\python.exe scripts/collect_licenses.py
```

---

## Giấy phép & Thông báo pháp lý

- Mã nguồn: [Giấy phép MIT](LICENSE).
- Ghi nhận mã nguồn mở & Miễn trừ bản quyền HoYoverse: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- Chính sách bảo mật: [SECURITY.md](SECURITY.md).
- Hướng dẫn đóng góp: [CONTRIBUTING.md](CONTRIBUTING.md).
